"""Screen-capture event detector for the gaming PC.

Grabs the screen ~10 times a second (it only reads pixels like OBS does and
never touches the game process), looks for on-screen messages using template
matching, confirms each event across several frames, and publishes it to the
Pi over MQTT. It also reports which app is in focus, so the companion knows
gaming from studying.

How detection works:
  1. pc/templates/<event>/*.png hold small cropped images of the on-screen
     message for each event (e.g. the "VICTORY ROYALE" text). Make them from
     your own screenshots, see pc/templates/README.md.
  2. Each frame is converted to grayscale and resized so its HUD is a fixed
     size (WORK_HEIGHT), so any screen resolution looks the same to the
     matcher. Then cv2.matchTemplate slides each template over the event's search
     region at a few scales and returns a 0-1 similarity score.
  3. A score above the event's threshold counts as "seen". ConfirmFilter only
     reports an event once it's seen in 2 of the last 3 frames, and a banner
     that stays up for several seconds counts once.
  4. Kills are counted, not just seen: the kill banner's level ("DOUBLE ELIM!"
     = 2) and the number of stacked "ENEMY ELIMINATED" popups give how many
     kills are on screen, and KillCounter sends one "elim" per new kill, so
     back-to-back kills are both counted.

Speed matters: a short kill popup is only on screen for ~1-2 s, so the loop
has to run several times a second. Templates are matched in parallel threads,
are pre-scaled once at startup, and the slow full-screen checks (death screen,
victory) only run every few frames because those screens stay up for seconds.

Usage:
  python detector.py --broker <PI_IP>                  # run live
  python detector.py --broker <PI_IP> --debug          # also print scores
  python detector.py --save-every 2                    # save a screenshot every 2 s
  python detector.py --test recordings                 # score saved screenshots offline
  python detector.py --crop shot.png 100 200 400 250 --event elim --name banner
                                                       # save a template (any resolution)

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import argparse
import fnmatch
import glob
import json
import os
import sys
import time

import cv2
import numpy as np

T_GAME = "desk/game/event"
T_ACTIVITY = "desk/pc/activity"

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "templates")

EVENTS = ["match_start", "elim", "knocked", "eliminated", "victory"]

# Where on screen to look for each event, as fractions of the screen
# (left, top, width, height). The default is a wide center area; tighten a
# region once you know where the message appears - it's faster and avoids
# false matches.
CENTER = (0.15, 0.05, 0.70, 0.85)
SEARCH_REGIONS = {event: CENTER for event in EVENTS}
SEARCH_REGIONS["elim"] = (0.15, 0.05, 0.85, 0.85)  # "+300 ENEMY ELIMINATED" pops up at the right edge
SEARCH_REGIONS["eliminated"] = (0.15, 0.0, 0.70, 0.90)  # "ELIMINATED BY" sits at the very top edge
SEARCH_REGIONS["knocked"] = (0.0, 0.85, 0.30, 0.15)   # red health bar + X icon, bottom-left

# Some messages always appear in the same spot, so their templates only search
# a small box there (much faster than the whole event region, and fewer false
# matches). Patterns match "<event>/<file name>"; others use SEARCH_REGIONS.
KILL_POPUP = (0.70, 0.30, 0.30, 0.40)   # "+300 ENEMY ELIMINATED" stack, right side
KILL_BANNER = (0.30, 0.62, 0.40, 0.25)  # "ELIMINATION!" / "DOUBLE ELIM!", bottom centre
TEMPLATE_REGIONS = [
    ("elim/*enemy_eliminated*", KILL_POPUP),
    ("elim/*elimination_banner*", KILL_BANNER),
    ("elim/*double_elim*", KILL_BANNER),
    ("elim/*triple_elim*", KILL_BANNER),
    ("elim/*quad_elim*", KILL_BANNER),
    ("victory/*victory_badge*", (0.30, 0.0, 0.40, 0.30)),  # "#1 VICTORY ROYALE" badge, top centre
    ("match_start/*", (0.42, 0.42, 0.36, 0.26)),  # "Space TO JUMP" on the bus, right of the crosshair
]

# Templates of white HUD text drawn straight over the game world (no dark
# plate behind it) are matched on a "white pixels only" mask instead of plain
# grayscale. Otherwise the map scrolling under the bus changes the score from
# frame to frame (0.58-0.85 for the same prompt) and the event gets missed.
WHITE_TEXT = ["match_start/*"]
WHITE_LEVEL = 225  # gray level that counts as white text

# How many kills a banner means: "DOUBLE ELIM!" = 2, etc. Matched by file name;
# any other elim template means 1. Popups ("ENEMY ELIMINATED") are counted
# instead: each stacked popup is one kill.
KILL_LEVELS = [("*quad*", 4), ("*triple*", 3), ("*double*", 2)]
POPUP_PATTERN = "elim/*enemy_eliminated*"

# A score this high is trusted from a single frame (frames without the
# message stay under ~0.75), see ConfirmFilter.
STRONG_SCORE = 0.88

# Similarity needed to count as "seen" (0-1). Tune with --test / --debug.
THRESHOLDS = {event: 0.80 for event in EVENTS}
# Kill templates search small boxes (TEMPLATE_REGIONS). Live kills score ~0.8+,
# frames without a kill stay under ~0.70 in those boxes.
THRESHOLDS["elim"] = 0.76
# White-text jump prompt: bus frames score 0.83-0.92, the look-alike
# "Space TO HURDLE" prompt in the same spot scores up to ~0.76.
THRESHOLDS["match_start"] = 0.82
# Once a kill message is on screen it only counts as gone when its score falls
# this far below the threshold, so one blurry frame doesn't make a held banner
# look like two kills.
KILL_HOLD_MARGIN = 0.12

# Frames (out of the last frames+1) an event must be seen in before it's sent.
# The bus prompt stays up for a long time and a false match_start would reset
# the match on the Pi, so it needs more evidence.
CONFIRM_FRAMES = {event: 2 for event in EVENTS}
CONFIRM_FRAMES["match_start"] = 3

# Seconds before the same event can be sent again. The bus prompt is up for
# 10-40 s and can drop out for a moment (the bus turns, a teammate's marker
# covers it), so match_start is locked out for a while - a new match can't
# start that soon anyway.
COOLDOWNS = {event: 3.0 for event in EVENTS}
COOLDOWNS["match_start"] = 90.0

# These screens stay up for seconds, so they're only checked every
# SLOW_EVERY frames - their big search areas are the most expensive part.
SLOW_EVENTS = {"eliminated", "victory"}
SLOW_EVERY = 3

# Resolution independence: Fortnite sizes its HUD to the largest 16:9 area
# that fits the screen, so the HUD scales with min(height, width * 9/16)
# (on a 16:10 laptop that's the width, not the height). Templates are stored
# as if that size were TEMPLATE_HEIGHT, and every frame is resized so it is
# WORK_HEIGHT before matching. So a 1920x1080 PC and a 2880x1800 laptop share
# screenshots and templates. Use --crop to save templates; it rescales.
TEMPLATE_HEIGHT = 1080
WORK_HEIGHT = 900           # matching resolution; lower = faster, less precise
# Small text matches only within ~3% of its size. Templates cut on the laptop
# match PC frames best at 0.95 and the other way around at 1.05; 0.9/1.1 never
# won on the recordings and cost 40% more time.
SCALES = (0.95, 1.0, 1.05)


# ---------- image helpers ----------
def _gray(img_bgr):
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if img_bgr.ndim == 3 else img_bgr


def _resize(gray, scale):
    if scale == 1.0:
        return gray
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interp)


def hud_height(shape):
    """Height of the 16:9 area the HUD is laid out in (see above)."""
    h, w = shape[:2]
    return min(h, w * 9 / 16)


def prep_frame(frame_bgr):
    """Full screenshot -> grayscale, scaled so its HUD is WORK_HEIGHT."""
    return _resize(_gray(frame_bgr), WORK_HEIGHT / hud_height(frame_bgr.shape))


def prep_template(img_bgr):
    """Template stored at TEMPLATE_HEIGHT scale -> same scale as prep_frame output."""
    return _resize(_gray(img_bgr), WORK_HEIGHT / TEMPLATE_HEIGHT)


def save_template(frame_path, box, event, name, folder=None):
    """Crops box=(x0, y0, x1, y1) in the screenshot's own pixels and saves it,
    rescaled to TEMPLATE_HEIGHT, as templates/<event>/<name>.png."""
    frame = cv2.imread(frame_path)
    if frame is None:
        raise SystemExit(f"can't read {frame_path}")
    x0, y0, x1, y1 = box
    crop = frame[y0:y1, x0:x1]
    scale = TEMPLATE_HEIGHT / hud_height(frame.shape)
    if scale != 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    out_dir = os.path.join(folder or TEMPLATE_DIR, event)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name if name.endswith(".png") else name + ".png")
    cv2.imwrite(out, crop)
    return out


def crop_frac(img, region):
    h, w = img.shape[:2]
    left, top, width, height = region
    x0, y0 = int(left * w), int(top * h)
    return img[y0:y0 + int(height * h), x0:x0 + int(width * w)]


def _matches(event, filename, patterns):
    key = f"{event}/{filename}"
    return any(fnmatch.fnmatch(key, p) for p in patterns)


def template_region(event, filename):
    key = f"{event}/{filename}"
    for pattern, region in TEMPLATE_REGIONS:
        if fnmatch.fnmatch(key, pattern):
            return region
    return SEARCH_REGIONS[event]


def white_mask(gray):
    """Keeps only bright text pixels (blurred a little so 1-px shifts still match)."""
    return cv2.GaussianBlur((gray >= WHITE_LEVEL).astype(np.float32), (3, 3), 0)


class Template:
    """One template image, pre-scaled to every size in SCALES."""

    def __init__(self, event, name, gray):
        self.event, self.name = event, name
        self.region = template_region(event, name)
        self.white = _matches(event, name, WHITE_TEXT)
        self.popup = event == "elim" and _matches(event, name, [POPUP_PATTERN])
        self.level = 1
        for pattern, level in KILL_LEVELS:
            if fnmatch.fnmatch(name, pattern):
                self.level = level
                break
        base = white_mask(gray) if self.white else gray
        self.scaled = []
        for s in SCALES:
            t = base if s == 1.0 else cv2.resize(base, None, fx=s, fy=s)
            if t.std() > (0.01 if self.white else 1):  # blank template would divide by zero
                self.scaled.append(t)


def load_templates(folder=TEMPLATE_DIR):
    """Returns {event: [Template, ...]} for events that have templates."""
    templates = {}
    for event in EVENTS:
        tmpls = []
        for path in sorted(glob.glob(os.path.join(folder, event, "*.png"))):
            img = cv2.imread(path)
            if img is not None:
                tmpls.append(Template(event, os.path.basename(path), prep_template(img)))
        if tmpls:
            templates[event] = tmpls
    return templates


def _match(region_img, tmpl, peak_min=None):
    """Best score of one template over its region (all scales). If peak_min is
    given, also returns every separate spot scoring >= peak_min as
    [(y, score)] - used to count stacked kill popups."""
    best, best_map, best_h = 0.0, None, 0
    for t in tmpl.scaled:
        if t.shape[0] > region_img.shape[0] or t.shape[1] > region_img.shape[1]:
            continue
        result = cv2.matchTemplate(region_img, t, cv2.TM_CCOEFF_NORMED)
        score = float(result.max())
        if score > best:
            best, best_map, best_h = score, result, t.shape[0]
    peaks = []
    if peak_min is not None and best_map is not None:
        r = best_map.copy()
        for _ in range(6):
            _, score, _, (x, y) = cv2.minMaxLoc(r)
            if score < peak_min:
                break
            peaks.append((y, score))
            r[max(0, y - best_h // 2):y + best_h // 2 + 1, :] = -1  # one popup per row band
    return best, peaks, best_h


def red_fraction(frame_bgr, region):
    """Share of strongly red pixels in a region. The knocked health bar is red,
    a healthy one is green, and in grayscale the two look almost the same."""
    b, g, r = cv2.split(crop_frac(frame_bgr, region).astype(np.int16))
    return float(((r > 150) & (g < 90) & (r - b > 60)).mean())


KNOCKED_MIN_RED = 0.02  # the red bar fills ~5% of the knocked search region


def _count_popups(peaks):
    """peaks: [(y, score, height)] from all popup templates (region pixels).
    Different templates find the same popup, so merge spots on the same row."""
    rows = []
    for y, score, h in sorted(peaks, key=lambda p: -p[1]):
        if all(abs(y - ry) > 0.6 * max(h, rh) for ry, rh in rows):
            rows.append((y, h))
    return len(rows)


class Detection(dict):
    """{event: best score} plus kill counting info:
    kills      - kills on screen right now (banner level / popup count)
    kills_hold - same, at the lower "still there" threshold
    kill_score - best elim template score."""
    kills = kills_hold = 0
    kill_score = 0.0


def detect(frame_bgr, templates, pool=None, skip=()):
    """Returns a Detection ({event: best score}) for every event with templates,
    except events in `skip`. `pool` (a ThreadPoolExecutor) matches templates
    in parallel - OpenCV releases the GIL, so this scales with CPU cores."""
    gray = prep_frame(frame_bgr)
    thr, hold = THRESHOLDS["elim"], THRESHOLDS["elim"] - KILL_HOLD_MARGIN
    crops = {}
    jobs = []
    for event, tmpls in templates.items():
        if event in skip:
            continue
        for tmpl in tmpls:
            key = (tmpl.region, tmpl.white)
            if key not in crops:
                c = crop_frac(gray, tmpl.region)
                crops[key] = white_mask(c) if tmpl.white else c
            jobs.append((tmpl, crops[key], hold if tmpl.popup else None))

    run = lambda job: _match(job[1], job[0], job[2])  # noqa: E731
    results = list(pool.map(run, jobs)) if pool else [run(j) for j in jobs]

    scores = Detection()
    popup_peaks = []
    level = level_hold = 0
    for (tmpl, _, _), (score, peaks, h) in zip(jobs, results):
        scores[tmpl.event] = max(scores.get(tmpl.event, 0.0), score)
        if tmpl.event != "elim":
            continue
        if tmpl.popup:
            popup_peaks += [(y, s, h) for y, s in peaks]
        else:
            level = max(level, tmpl.level if score >= thr else 0)
            level_hold = max(level_hold, tmpl.level if score >= hold else 0)
    if "elim" in scores:
        scores.kill_score = scores["elim"]
        scores.kills = max(level, _count_popups([p for p in popup_peaks if p[1] >= thr]))
        scores.kills_hold = max(level_hold, _count_popups(popup_peaks), scores.kills)

    # Knocked needs the bar to actually be red, not just bar-shaped
    if "knocked" in scores and red_fraction(frame_bgr, SEARCH_REGIONS["knocked"]) < KNOCKED_MIN_RED:
        scores["knocked"] = min(scores["knocked"], 0.5)
    return scores


class ConfirmFilter:
    """Only report an event once it's seen in `frames` of the last frames+1
    checks (so one bad frame doesn't reset it), or right away if it's seen with
    a very high score (`strong` events) - a short banner may only land in one
    frame. Each appearance is reported once: it only counts as gone after
    `end_misses` checks in a row without it (one blurry frame in a 10 s victory
    screen must not make it fire twice), and the same event is ignored for
    `cooldown` seconds afterward. `frames` and `cooldown` can be a number or
    {event: number}."""

    def __init__(self, frames=2, cooldown=3.0, end_misses=2):
        self.frames = frames
        self.cooldown = cooldown
        self.end_misses = end_misses
        self.history = {}    # event -> recent seen/not-seen checks
        self.misses = {}
        self.fired = set()   # events already reported for the current appearance
        self.last_sent = {}

    def _need(self, name):
        return self.frames.get(name, 2) if isinstance(self.frames, dict) else self.frames

    def _cooldown(self, name):
        return self.cooldown.get(name, 3.0) if isinstance(self.cooldown, dict) else self.cooldown

    def update(self, seen, now, strong=(), checked=None):
        """seen: event names detected in this frame; strong: the subset seen
        with a score high enough to trust a single frame; checked: events that
        were actually looked for this frame (default: all), so skipped slow
        checks don't count as misses. Returns confirmed events."""
        confirmed = []
        names = set(self.history) | set(seen)
        for name in names:
            if checked is not None and name not in checked:
                continue
            need = self._need(name)
            hist = self.history.setdefault(name, [])
            hist.append(name in seen)
            del hist[:-(need + 1)]
            if name not in seen:
                self.misses[name] = self.misses.get(name, 0) + 1
                if self.misses[name] >= self.end_misses:
                    self.fired.discard(name)
                continue
            self.misses[name] = 0
            if name in self.fired:
                continue
            last = self.last_sent.get(name)
            recently = last is not None and now - last < self._cooldown(name)
            if (sum(hist) >= need or name in strong) and not recently:
                confirmed.append(name)
                self.fired.add(name)
                self.last_sent[name] = now
        return confirmed


class KillCounter:
    """Turns "how many kills are on screen" into one event per new kill.

    The count goes up when a banner turns into "DOUBLE ELIM!" or another
    "ENEMY ELIMINATED" popup stacks up, so two kills a split second apart are
    both counted (the old filter saw one long banner and a 3 s cooldown).
    A new count must be seen in 2 frames in a row unless its score is strong.
    The count only drops back once the kill messages have been gone (at the
    lower hold threshold) for `settle` seconds, so a flickering banner isn't
    counted twice, but a fresh kill after the old popups faded is."""

    def __init__(self, settle=0.4):
        self.settle = settle
        self.level = 0        # kills already reported for what's on screen
        self.prev = 0         # count seen in the previous frame
        self.low_since = None

    def update(self, count, now, strong=False, hold=None):
        hold = count if hold is None else max(hold, count)
        candidate = count if strong else min(count, self.prev)
        self.prev = count
        new = 0
        if candidate > self.level:
            new = candidate - self.level
            self.level = candidate
            self.low_since = None
        elif hold < self.level:
            if self.low_since is None:
                self.low_since = now
            if now - self.low_since >= self.settle:
                self.level = hold
                self.low_since = None
        else:
            self.low_since = None
        return new


# ---------- PC activity (Windows) ----------
def active_window_title():
    if sys.platform != "win32":
        return None
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def idle_seconds():
    """Seconds since the last keyboard/mouse input (Windows only)."""
    if sys.platform != "win32":
        return None
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def activity():
    title = active_window_title() or ""
    return {"app": title[:80], "gaming": "fortnite" in title.lower(), "idle_s": idle_seconds()}


# ---------- modes ----------
def make_pool():
    from concurrent.futures import ThreadPoolExecutor
    return ThreadPoolExecutor(max_workers=max(1, min(8, (os.cpu_count() or 2) - 1)))


def run_test(path, templates):
    """Score saved screenshots so thresholds and templates can be tuned offline."""
    files = sorted(glob.glob(os.path.join(path, "*.png"))) if os.path.isdir(path) else [path]
    if not files:
        print(f"no .png files at {path}")
        return
    pool = make_pool()
    for f in files:
        img = cv2.imread(f)
        if img is None:
            print(f"{f}: can't read")
            continue
        start = time.time()
        scores = detect(img, templates, pool)
        ms = (time.time() - start) * 1000
        hits = [e for e, s in scores.items() if s >= THRESHOLDS[e]]
        if "elim" in hits and scores.kills > 1:
            hits[hits.index("elim")] = f"elim x{scores.kills}"
        detail = "  ".join(f"{e}={s:.2f}" for e, s in scores.items())
        print(f"{os.path.basename(f)}: {detail}   -> {', '.join(hits) or 'nothing'}  ({ms:.0f} ms)")


def run_live(args, templates):
    import mss
    import paho.mqtt.client as mqtt

    client = None
    if args.broker:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="desk-detector")
        client.connect(args.broker, args.port)
        client.loop_start()
    else:
        print("no --broker given: detecting only, nothing is sent")

    def publish(topic, payload):
        if client:
            client.publish(topic, json.dumps(payload))

    confirm = ConfirmFilter(frames=CONFIRM_FRAMES, cooldown=COOLDOWNS)
    kills = KillCounter()
    pool = make_pool()
    saver = None
    if args.save_every:
        os.makedirs(args.save_dir, exist_ok=True)
        # Save screenshots on a background thread so slow PNG writes don't
        # slow down detection (fewer frames = short banners get missed)
        from concurrent.futures import ThreadPoolExecutor
        saver = ThreadPoolExecutor(max_workers=1)
    last_save = last_activity = last_debug = 0.0
    frame_no = 0
    loop_times = []

    with mss.mss() as sct:
        monitor = sct.monitors[args.monitor]
        print(f"capturing monitor {args.monitor} ({monitor['width']}x{monitor['height']}) "
              f"at up to {args.fps} fps; events with templates: {', '.join(templates) or 'none'}")
        while True:
            start = time.time()
            frame = cv2.cvtColor(np.array(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)

            if templates:
                skip = SLOW_EVENTS if frame_no % SLOW_EVERY else set()
                frame_no += 1
                scores = detect(frame, templates, pool, skip=skip)
                # Kills are counted separately so back-to-back kills each count
                if "elim" in scores:
                    n = kills.update(scores.kills, start, strong=scores.kill_score >= STRONG_SCORE,
                                     hold=scores.kills_hold)
                    for _ in range(n):
                        publish(T_GAME, {"type": "elim"})
                    if n:
                        print(f"EVENT: elim x{n}  (on screen {scores.kills}, score {scores.kill_score:.2f})")
                others = {e: s for e, s in scores.items() if e != "elim"}
                seen = {e for e, s in others.items() if s >= THRESHOLDS[e]}
                strong = {e for e in seen if others[e] >= STRONG_SCORE}
                for event in confirm.update(seen, start, strong, checked=set(others)):
                    publish(T_GAME, {"type": event})
                    print(f"EVENT: {event}  (score {scores[event]:.2f})")
                if args.debug and start - last_debug >= 1:
                    last_debug = start
                    fps = len(loop_times) / max(sum(loop_times), 1e-6)
                    loop_times.clear()
                    print(f"  [{fps:.1f} fps] " + "  ".join(f"{e}={s:.2f}" for e, s in scores.items())
                          + (f"  kills on screen={scores.kills}" if "elim" in scores else ""))

            if start - last_activity >= 5:
                last_activity = start
                publish(T_ACTIVITY, activity())

            if args.save_every and start - last_save >= args.save_every:
                last_save = start
                saver.submit(cv2.imwrite, os.path.join(args.save_dir, f"frame_{int(start * 1000)}.png"),
                             frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])  # fast save, bigger file

            time.sleep(max(0.0, 1.0 / args.fps - (time.time() - start)))
            loop_times.append(time.time() - start)


def main():
    parser = argparse.ArgumentParser(description="Fortnite screen event detector")
    parser.add_argument("--broker", help="Pi IP address (omit to detect without sending)")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--monitor", type=int, default=1, help="1 = primary screen")
    parser.add_argument("--debug", action="store_true", help="print match scores and real fps once a second")
    parser.add_argument("--save-every", type=float, default=0,
                        help="save a full screenshot every N seconds (for making templates)")
    parser.add_argument("--save-dir", default=os.path.join(HERE, "recordings"))
    parser.add_argument("--test", metavar="PATH", help="score a saved screenshot or a folder of them")
    parser.add_argument("--crop", nargs=5, metavar=("SHOT", "X0", "Y0", "X1", "Y1"),
                        help="save a template: box in the screenshot's own pixels (needs --event, --name)")
    parser.add_argument("--event", choices=EVENTS, help="template folder for --crop")
    parser.add_argument("--name", help="template file name for --crop")
    args = parser.parse_args()

    if args.crop:
        if not (args.event and args.name):
            parser.error("--crop needs --event and --name")
        shot, *box = args.crop
        print("saved", save_template(shot, tuple(int(v) for v in box), args.event, args.name))
        return

    templates = load_templates()
    if not templates:
        print(f"no templates found in {TEMPLATE_DIR} - see templates/README.md")

    if args.test:
        run_test(args.test, templates)
    else:
        run_live(args, templates)


if __name__ == "__main__":
    main()

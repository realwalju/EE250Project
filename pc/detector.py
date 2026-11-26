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
     reports an event after 2 consecutive frames, and a banner that stays up
     for several seconds counts once.

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
]

# Similarity needed to count as "seen" (0-1). Tune with --test / --debug.
THRESHOLDS = {event: 0.80 for event in EVENTS}
# Real kills score 0.91+, busy non-kill scenes reach ~0.75 on the elim templates
THRESHOLDS["elim"] = 0.84

# Resolution independence: Fortnite sizes its HUD to the largest 16:9 area
# that fits the screen, so the HUD scales with min(height, width * 9/16)
# (on a 16:10 laptop that's the width, not the height). Templates are stored
# as if that size were TEMPLATE_HEIGHT, and every frame is resized so it is
# WORK_HEIGHT before matching. So a 1920x1080 PC and a 2880x1800 laptop share
# screenshots and templates. Use --crop to save templates; it rescales.
TEMPLATE_HEIGHT = 1080
WORK_HEIGHT = 900           # matching resolution; lower = faster, less precise
SCALES = (0.9, 0.95, 1.0, 1.05, 1.1)  # small text matches only within ~3% of its size


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


def template_region(event, filename):
    key = f"{event}/{filename}"
    for pattern, region in TEMPLATE_REGIONS:
        if fnmatch.fnmatch(key, pattern):
            return region
    return SEARCH_REGIONS[event]


def load_templates(folder=TEMPLATE_DIR):
    """Returns {event: [(grayscale template at matching scale, search region), ...]}
    for events that have templates."""
    templates = {}
    for event in EVENTS:
        imgs = []
        for path in sorted(glob.glob(os.path.join(folder, event, "*.png"))):
            img = cv2.imread(path)
            if img is not None:
                imgs.append((prep_template(img), template_region(event, os.path.basename(path))))
        if imgs:
            templates[event] = imgs
    return templates


def best_score(region_gray, template):
    best = 0.0
    for s in SCALES:
        t = template if s == 1.0 else cv2.resize(template, None, fx=s, fy=s)
        if t.shape[0] > region_gray.shape[0] or t.shape[1] > region_gray.shape[1]:
            continue
        if t.std() < 1:  # blank template - matchTemplate would divide by zero
            continue
        result = cv2.matchTemplate(region_gray, t, cv2.TM_CCOEFF_NORMED)
        best = max(best, float(result.max()))
    return best


def red_fraction(frame_bgr, region):
    """Share of strongly red pixels in a region. The knocked health bar is red,
    a healthy one is green, and in grayscale the two look almost the same."""
    b, g, r = cv2.split(crop_frac(frame_bgr, region).astype(np.int16))
    return float(((r > 150) & (g < 90) & (r - b > 60)).mean())


KNOCKED_MIN_RED = 0.02  # the red bar fills ~5% of the knocked search region


def detect(frame_bgr, templates):
    """Returns {event: best similarity score} for every event with templates."""
    gray = prep_frame(frame_bgr)
    scores = {}
    crops = {}
    for event, tmpls in templates.items():
        best = 0.0
        for tmpl, region in tmpls:
            if region not in crops:
                crops[region] = crop_frac(gray, region)
            best = max(best, best_score(crops[region], tmpl))
        scores[event] = best
    # Knocked needs the bar to actually be red, not just bar-shaped
    if "knocked" in scores and red_fraction(frame_bgr, SEARCH_REGIONS["knocked"]) < KNOCKED_MIN_RED:
        scores["knocked"] = min(scores["knocked"], 0.5)
    return scores


class ConfirmFilter:
    """Only report an event after it's seen in `frames` consecutive frames.
    Each appearance is reported once (a banner that stays up for 4 s is one
    event), and the same event is ignored for `cooldown` seconds afterward."""

    def __init__(self, frames=3, cooldown=3.0):
        self.frames = frames
        self.cooldown = cooldown
        self.streak = {}
        self.last_sent = {}

    def update(self, seen, now):
        """seen: set of event names detected in this frame. Returns confirmed events."""
        confirmed = []
        for name in list(self.streak):
            if name not in seen:
                self.streak[name] = 0
        for name in seen:
            self.streak[name] = self.streak.get(name, 0) + 1
            last = self.last_sent.get(name)
            recently = last is not None and now - last < self.cooldown
            if self.streak[name] == self.frames and not recently:
                confirmed.append(name)
                self.last_sent[name] = now
        return confirmed


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
def run_test(path, templates):
    """Score saved screenshots so thresholds and templates can be tuned offline."""
    files = sorted(glob.glob(os.path.join(path, "*.png"))) if os.path.isdir(path) else [path]
    if not files:
        print(f"no .png files at {path}")
        return
    for f in files:
        img = cv2.imread(f)
        if img is None:
            print(f"{f}: can't read")
            continue
        scores = detect(img, templates)
        hits = [e for e, s in scores.items() if s >= THRESHOLDS[e]]
        detail = "  ".join(f"{e}={s:.2f}" for e, s in scores.items())
        print(f"{os.path.basename(f)}: {detail}   -> {', '.join(hits) or 'nothing'}")


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

    confirm = ConfirmFilter(frames=2, cooldown=3.0)
    if args.save_every:
        os.makedirs(args.save_dir, exist_ok=True)
    last_save = last_activity = last_debug = 0.0

    with mss.mss() as sct:
        monitor = sct.monitors[args.monitor]
        print(f"capturing monitor {args.monitor} ({monitor['width']}x{monitor['height']}) "
              f"at {args.fps} fps; events with templates: {', '.join(templates) or 'none'}")
        while True:
            start = time.time()
            frame = cv2.cvtColor(np.array(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)

            if templates:
                scores = detect(frame, templates)
                seen = {e for e, s in scores.items() if s >= THRESHOLDS[e]}
                for event in confirm.update(seen, start):
                    publish(T_GAME, {"type": event})
                    print(f"EVENT: {event}  (score {scores[event]:.2f})")
                if args.debug and start - last_debug >= 1:
                    last_debug = start
                    print("  " + "  ".join(f"{e}={s:.2f}" for e, s in scores.items()))

            if start - last_activity >= 5:
                last_activity = start
                publish(T_ACTIVITY, activity())

            if args.save_every and start - last_save >= args.save_every:
                last_save = start
                cv2.imwrite(os.path.join(args.save_dir, f"frame_{int(start * 1000)}.png"), frame,
                            [cv2.IMWRITE_PNG_COMPRESSION, 1])  # fast save, bigger file

            time.sleep(max(0.0, 1.0 / args.fps - (time.time() - start)))


def main():
    parser = argparse.ArgumentParser(description="Fortnite screen event detector")
    parser.add_argument("--broker", help="Pi IP address (omit to detect without sending)")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--monitor", type=int, default=1, help="1 = primary screen")
    parser.add_argument("--debug", action="store_true", help="print match scores once a second")
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

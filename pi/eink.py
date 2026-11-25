"""Draws the companion screen and sends it to the e-ink display.

Defaults to our Waveshare 7.3" Spectra 6 panel (driver epd7in3e). Another
Waveshare panel can be chosen with --eink-model or EINK_MODEL, and
--eink-model inky uses a Pimoroni Inky instead.

Layout: the companion's face on the left, its current message on the right,
session stats and room info along the bottom.

E-ink takes many seconds to refresh and flashes while it does, so refreshes
run in a background thread and are rate-limited (min_interval). Without an
display attached (e.g. on a laptop), it saves eink_preview.png instead,
so the layout can be checked anywhere.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import os
import textwrap
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

PREVIEW_PATH = os.path.join(os.path.dirname(__file__), "eink_preview.png")
DEFAULT_MODEL = "epd7in3e"  # Waveshare 7.3" E Ink Spectra 6, 800x480
DEFAULT_SIZE = (800, 480)  # used for the preview; the real size is read from the display

BLACK, WHITE = (0, 0, 0), (255, 255, 255)
# Pure colours that exist as inks on 6/7-colour e-paper (Spectra 6: black,
# white, yellow, red, blue, green). Anything else gets snapped to these.
RED, BLUE, GREEN, YELLOW = (255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 255, 0)
FACE_FILL = YELLOW

# Colour of the mood label (never yellow: unreadable on white)
ACCENT = {"happy": GREEN, "hype": RED, "proud": GREEN, "starstruck": RED,
          "smug": RED, "shocked": RED, "sad": BLUE, "worried": BLUE, "angry": RED,
          "focused": BLUE, "sleepy": BLUE, "calm": BLUE, "neutral": BLACK}
MOODS = list(ACCENT)

def _font(size, bold=False):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    dirs = ["/usr/share/fonts/truetype/dejavu", ""]
    for d in dirs:
        for n in names:
            try:
                return ImageFont.truetype(os.path.join(d, n) if d else n, size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)


def _star(d, x, y, r, fill):
    import math
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.45
        ang = -math.pi / 2 + i * math.pi / 5
        pts.append((x + rr * math.cos(ang), y + rr * math.sin(ang)))
    d.polygon(pts, fill=fill, outline=BLACK)


def _drop(d, x, y, r, fill):
    """Teardrop / sweat drop pointing up."""
    d.ellipse([x - r, y, x + r, y + 2 * r], fill=fill, outline=BLACK, width=2)
    d.polygon([(x - r * 0.85, y + r * 0.6), (x, y - r * 1.3), (x + r * 0.85, y + r * 0.6)], fill=fill)
    d.line([(x - r * 0.85, y + r * 0.6), (x, y - r * 1.3), (x + r * 0.85, y + r * 0.6)], fill=BLACK, width=2)


def draw_face(d, cx, cy, r, mood):
    """The companion: a round face built from simple shapes so it reads clearly
    on e-ink. Each mood changes the eyes, brows, mouth and extras."""
    lw = max(3, int(r * 0.045))       # line width
    ex, ey = r * 0.36, cy - r * 0.18  # eye x offset, eye height
    er = r * 0.11                     # eye radius
    my = cy + r * 0.35                # mouth height

    # Extras behind the face
    if mood == "proud":  # crown
        cw, ch = r * 0.75, r * 0.45
        top = cy - r * 0.92 - ch
        pts = [(cx - cw, cy - r * 0.8), (cx - cw, top + ch * 0.25), (cx - cw * 0.5, top + ch * 0.6),
               (cx, top), (cx + cw * 0.5, top + ch * 0.6), (cx + cw, top + ch * 0.25), (cx + cw, cy - r * 0.8)]
        d.polygon(pts, fill=YELLOW, outline=BLACK, width=lw)
        for px, py in pts[1:-1:2] + [pts[3]]:
            d.ellipse([px - lw * 1.6, py - lw * 1.6, px + lw * 1.6, py + lw * 1.6], fill=RED, outline=BLACK)

    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=FACE_FILL, outline=BLACK, width=lw + 2)

    def eyes_dots(scale=1.0):
        for sx in (-1, 1):
            x, rr = cx + sx * ex, er * scale
            d.ellipse([x - rr, ey - rr * 1.25, x + rr, ey + rr * 1.25], fill=BLACK)

    def eyes_arcs(up=True):  # ^ ^ (happy) or closed u u (sleepy)
        for sx in (-1, 1):
            x = cx + sx * ex
            box = [x - er * 1.6, ey - er * 1.2, x + er * 1.6, ey + er * 1.4]
            d.arc(box, 180, 360, fill=BLACK, width=lw) if up else d.arc(box, 0, 180, fill=BLACK, width=lw)

    def brows(inner_dy, outer_dy, color=BLACK):  # negative = higher
        for sx in (-1, 1):
            x = cx + sx * ex
            inner = (x - sx * er * 1.6, ey - er * 2.4 + inner_dy * er)
            outer = (x + sx * er * 1.6, ey - er * 2.4 + outer_dy * er)
            d.line([inner, outer], fill=color, width=lw + 2)

    def smile(width=0.5, depth=0.35, fill=None):
        box = [cx - r * width, my - r * depth, cx + r * width, my + r * depth * 0.6]
        if fill:
            d.chord(box, 0, 180, fill=fill, outline=BLACK, width=lw)
        else:
            d.arc(box, 15, 165, fill=BLACK, width=lw)

    def frown(width=0.4):
        d.arc([cx - r * width, my, cx + r * width, my + r * 0.5], 200, 340, fill=BLACK, width=lw)

    if mood == "happy":
        eyes_dots(); smile(0.45, 0.3, fill=RED)
    elif mood == "hype":
        eyes_arcs(up=True); brows(-0.6, -0.6)
        smile(0.55, 0.45, fill=RED)
        d.line([cx - r * 0.42, my + r * 0.05, cx + r * 0.42, my + r * 0.05], fill=WHITE, width=lw * 2)
        for sx in (-1, 1):  # motion lines
            x = cx + sx * r * 1.12
            for dy in (-0.35, 0, 0.35):
                d.line([x, cy + dy * r, x + sx * r * 0.22, cy + dy * r * 1.2], fill=RED, width=lw)
    elif mood == "proud":
        eyes_arcs(up=True); smile(0.45, 0.3, fill=RED)
    elif mood == "starstruck":
        for sx in (-1, 1):
            _star(d, cx + sx * ex, ey, er * 2.2, RED)
        smile(0.5, 0.4, fill=RED)
    elif mood == "smug":
        for sx in (-1, 1):  # half-lidded
            x = cx + sx * ex
            d.chord([x - er * 1.3, ey - er * 1.3, x + er * 1.3, ey + er * 1.3], 0, 180, fill=BLACK)
            d.line([x - er * 1.7, ey, x + er * 1.7, ey], fill=BLACK, width=lw)
        brows(0.4, -0.4)
        d.arc([cx - r * 0.05, my - r * 0.3, cx + r * 0.5, my + r * 0.15], 20, 150, fill=BLACK, width=lw)
    elif mood == "shocked":
        for sx in (-1, 1):
            x = cx + sx * ex
            d.ellipse([x - er * 1.5, ey - er * 1.5, x + er * 1.5, ey + er * 1.5], fill=WHITE, outline=BLACK, width=lw)
            d.ellipse([x - er * 0.6, ey - er * 0.6, x + er * 0.6, ey + er * 0.6], fill=BLACK)
        brows(-1.2, -0.8)
        d.ellipse([cx - r * 0.17, my - r * 0.12, cx + r * 0.17, my + r * 0.3], fill=BLACK)
        for sx in (-1, 1):  # "!" marks
            x = cx + sx * r * 1.15
            d.line([x, cy - r * 0.9, x, cy - r * 0.45], fill=RED, width=lw * 2)
            d.ellipse([x - lw, cy - r * 0.33, x + lw, cy - r * 0.33 + 2 * lw], fill=RED)
    elif mood == "sad":
        eyes_dots(0.9); brows(-0.9, 0.4); frown(0.35)
        _drop(d, cx + ex + er * 0.3, ey + er * 1.6, er * 0.9, BLUE)
    elif mood == "worried":
        eyes_dots(); brows(-1.0, 0.3)
        d.line([cx - r * 0.3, my + r * 0.1, cx - r * 0.1, my + r * 0.02, cx + r * 0.1, my + r * 0.12,
                cx + r * 0.3, my + r * 0.04], fill=BLACK, width=lw, joint="curve")  # wobbly mouth
        _drop(d, cx + r * 0.78, cy - r * 0.6, r * 0.12, BLUE)
    elif mood == "angry":
        eyes_dots(0.9); brows(1.1, -0.5, color=BLACK)
        tw, th = r * 0.42, r * 0.22  # gritted teeth
        d.rounded_rectangle([cx - tw, my - th * 0.3, cx + tw, my + th], radius=th * 0.4,
                            fill=WHITE, outline=BLACK, width=lw)
        for i in range(1, 4):
            x = cx - tw + i * tw / 2
            d.line([x, my - th * 0.3, x, my + th], fill=BLACK, width=max(2, lw // 2))
        d.line([cx - tw, my + th * 0.35, cx + tw, my + th * 0.35], fill=BLACK, width=max(2, lw // 2))
        for sx in (-1, 1):  # red cheeks
            x = cx + sx * r * 0.62
            d.ellipse([x - r * 0.13, cy + r * 0.05, x + r * 0.13, cy + r * 0.2], fill=RED)
    elif mood == "focused":
        for sx in (-1, 1):  # narrowed eyes
            x = cx + sx * ex
            d.chord([x - er * 1.3, ey - er * 1.2, x + er * 1.3, ey + er * 1.3], 180, 360, fill=BLACK)
        brows(0.9, 0.2)
        d.line([cx - r * 0.25, my + r * 0.08, cx + r * 0.25, my + r * 0.05], fill=BLACK, width=lw + 1)
    elif mood == "sleepy":
        eyes_arcs(up=False)
        d.ellipse([cx - r * 0.09, my, cx + r * 0.09, my + r * 0.18], outline=BLACK, width=lw)
        zf = _font(int(r * 0.32), bold=True)
        d.text((cx + r * 0.75, cy - r * 1.0), "z", font=zf, fill=BLUE)
        d.text((cx + r * 1.0, cy - r * 1.35), "Z", font=zf, fill=BLUE)
    elif mood == "calm":
        eyes_arcs(up=False); smile(0.35, 0.25)
    else:  # neutral
        eyes_dots()
        d.line([cx - r * 0.28, my + r * 0.08, cx + r * 0.28, my + r * 0.08], fill=BLACK, width=lw + 1)


def render(state, size=DEFAULT_SIZE):
    """state: {"mood", "message", "session": {...}, "sensors": {...}, "study_mode"}"""
    w, h = size
    img = Image.new("RGB", size, WHITE)
    d = ImageDraw.Draw(img)
    mood = state.get("mood", "neutral")
    if mood not in ACCENT:
        mood = "neutral"
    color = ACCENT[mood]
    s = w / 800  # scale everything from the 800x480 design

    # Face
    face_r = int(125 * s)
    draw_face(d, int(170 * s), int(215 * s), face_r, mood)

    # Mood label + message
    x0 = int(340 * s)
    d.text((x0, int(40 * s)), mood.upper(), font=_font(int(26 * s), bold=True), fill=color)
    msg_font = _font(int(34 * s), bold=True)
    lines = textwrap.wrap(state.get("message", ""), width=22)[:6]
    y = int(90 * s)
    for line in lines:
        d.text((x0, y), line, font=msg_font, fill=BLACK)
        y += int(46 * s)

    # Bottom bar
    bar_y = h - int(70 * s)
    d.line([0, bar_y, w, bar_y], fill=BLACK, width=3)
    sess = state.get("session", {})
    sensors = state.get("sensors", {})
    temp = sensors.get("temp_c")
    temp_str = f"{temp * 9 / 5 + 32:.0f}F" if isinstance(temp, (int, float)) else "--"
    mode = "STUDY" if state.get("study_mode") else ("IN MATCH" if state.get("in_match") else "LOBBY")
    footer = (f"{sess.get('games', 0)} games   {sess.get('wins', 0)} wins   "
              f"{sess.get('elims', 0)} elims   |   room {temp_str}   |   {mode}")
    small = _font(int(22 * s))
    d.text((int(20 * s), bar_y + int(20 * s)), footer, font=small, fill=BLACK)
    clock = datetime.now().strftime("%I:%M %p").lstrip("0")
    cw = d.textlength(clock, font=small)
    d.text((w - cw - int(20 * s), bar_y + int(20 * s)), clock, font=small, fill=BLACK)
    return img


# Inks on Waveshare colour panels: "e" = Spectra 6 (e.g. epd7in3e), "f" = 7-colour (adds orange)
SPECTRA6 = [(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0), (0, 0, 255), (0, 255, 0)]
SEVEN_COLOR = SPECTRA6 + [(255, 128, 0)]


def snap_to_palette(img, colors):
    """Map every pixel to the nearest ink with no dithering, so solid shapes and
    anti-aliased text stay crisp instead of turning into speckles."""
    pal = Image.new("P", (1, 1))
    flat = [c for rgb in colors for c in rgb]
    pal.putpalette(flat + flat[:3] * (256 - len(colors)))
    return img.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")


class _Waveshare:
    """Waveshare e-paper via the official waveshare_epd library."""

    def __init__(self, model, color=None):
        import importlib
        self.epd = importlib.import_module(f"waveshare_epd.{model}").EPD()
        w, h = self.epd.width, self.epd.height
        self.rotate = h > w  # some panels are natively portrait: draw landscape, then rotate
        self.size = (h, w) if self.rotate else (w, h)
        # 7-colour / 6-colour panels end in "f" or "e" (epd7in3f, epd7in3e); the rest are black/white
        self.color = model.endswith(("f", "e")) if color is None else color
        self.palette = SEVEN_COLOR if model.endswith("f") else SPECTRA6
        self.name = f"Waveshare {model}"

    def show(self, img):
        if self.rotate:
            img = img.rotate(90, expand=True)
        if self.color:
            img = snap_to_palette(img, self.palette)
        else:
            # Threshold instead of dithering, so coloured parts become solid black
            img = img.convert("L").point(lambda p: 0 if p < 160 else 255, mode="1")
        self.epd.init()
        buf = self.epd.getbuffer(img)
        try:
            self.epd.display(buf)
        except TypeError:  # 3-colour (b/c) panels take a black and a red buffer
            self.epd.display(buf, self.epd.getbuffer(Image.new("1", img.size, 255)))
        self.epd.sleep()  # low-power mode between refreshes; protects the panel


class _Inky:
    """Pimoroni Inky, detected from the board's EEPROM."""

    def __init__(self):
        from inky.auto import auto
        self.inky = auto()
        self.size = self.inky.resolution
        self.name = type(self.inky).__name__

    def show(self, img):
        self.inky.set_image(img)
        self.inky.show()


class EinkDisplay:
    def __init__(self, min_interval=60.0, model=None, color=None):
        self.min_interval = min_interval
        self.last_refresh = 0.0
        self.pending = None
        self.pending_force = False
        self.busy = False
        self.timer = None  # wakes up a skipped (rate-limited) update later
        self.lock = threading.Lock()
        model = model or os.environ.get("EINK_MODEL") or DEFAULT_MODEL
        try:
            self.backend = _Inky() if model == "inky" else _Waveshare(model, color)
            self.size = self.backend.size
            print(f"e-ink: using {self.backend.name} {self.size}")
        except Exception as e:
            self.backend = None
            self.size = DEFAULT_SIZE
            print(f"e-ink: no display ({e.__class__.__name__}: {e}), saving {PREVIEW_PATH}")

    def request(self, state, force=False):
        """Queue a refresh. Skipped if one happened recently, unless force=True."""
        with self.lock:
            self.pending = state
            self.pending_force = self.pending_force or force
            if self.busy:
                return  # the running refresh will handle a forced update afterward
            wait = self.min_interval - (time.time() - self.last_refresh)
            if not self.pending_force and wait > 0:
                # Too soon: show it once the rate limit allows, so in-match
                # reactions still appear (just a little later).
                if self.timer is None:
                    self.timer = threading.Timer(wait, self._wake)
                    self.timer.daemon = True
                    self.timer.start()
                return
            self.busy = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _wake(self):
        with self.lock:
            self.timer = None
            if self.busy or self.pending is None:
                return
            self.busy = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        first = True
        while True:
            with self.lock:
                # After the first refresh, only go again for a forced update
                # (e.g. a match recap); normal updates wait for min_interval.
                if self.pending is None or (not first and not self.pending_force):
                    self.busy = False
                    return
                state, self.pending, self.pending_force = self.pending, None, False
            first = False
            img = render(state, self.size)
            try:
                if self.backend:
                    self.backend.show(img)
                else:
                    img.save(PREVIEW_PATH)
            except Exception as e:
                print(f"e-ink refresh failed: {e}")
            with self.lock:
                self.last_refresh = time.time()

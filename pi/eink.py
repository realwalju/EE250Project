"""Draws the companion screen and sends it to the e-ink display.

Supports Waveshare e-paper displays (pass the driver name, e.g. epd7in5_V2,
with --eink-model or the EINK_MODEL environment variable) and Pimoroni Inky
displays (detected automatically).

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
DEFAULT_SIZE = (800, 480)  # used for the preview; the real size is read from the display

BLACK, WHITE = (0, 0, 0), (255, 255, 255)
# Pure colours that exist as inks on 6/7-colour e-paper
ACCENT = {"happy": (0, 255, 0), "smug": (255, 0, 0), "worried": (255, 0, 0),
          "sleepy": (0, 0, 255), "calm": (0, 0, 255), "neutral": BLACK}


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


def draw_face(d, cx, cy, r, mood, color):
    """Simple shapes so it reads clearly on e-ink."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLACK, width=6)
    ex, ey, er = r * 0.38, cy - r * 0.2, r * 0.1  # eye offset, eye height, eye radius
    mouth = [cx - r * 0.45, cy + r * 0.05, cx + r * 0.45, cy + r * 0.6]

    if mood in ("sleepy", "calm"):  # closed eyes
        for sx in (-1, 1):
            x = cx + sx * ex
            d.arc([x - er * 1.5, ey - er, x + er * 1.5, ey + er * 1.5], 0, 180, fill=BLACK, width=5)
    elif mood == "smug":  # half-lidded
        for sx in (-1, 1):
            x = cx + sx * ex
            d.chord([x - er, ey - er, x + er, ey + er], 0, 180, fill=BLACK)
            d.line([x - er * 1.4, ey, x + er * 1.4, ey], fill=BLACK, width=5)
    else:
        for sx in (-1, 1):
            x = cx + sx * ex
            d.ellipse([x - er, ey - er, x + er, ey + er], fill=BLACK)

    if mood == "worried":  # slanted brows + frown
        for sx in (-1, 1):
            x = cx + sx * ex
            d.line([x - sx * er * 1.8, ey - er * 2.6, x + sx * er * 1.4, ey - er * 1.6], fill=color, width=6)
        d.arc([mouth[0], mouth[1] + r * 0.25, mouth[2], mouth[3] + r * 0.3], 200, 340, fill=color, width=6)
    elif mood == "happy":
        d.chord(mouth, 0, 180, fill=color)
    elif mood == "smug":
        d.arc([cx - r * 0.1, mouth[1], cx + r * 0.5, mouth[3] - r * 0.1], 20, 160, fill=color, width=6)
    elif mood == "sleepy":
        d.ellipse([cx - r * 0.1, cy + r * 0.3, cx + r * 0.1, cy + r * 0.5], outline=BLACK, width=4)
        zf = _font(int(r * 0.3), bold=True)
        d.text((cx + r * 0.7, cy - r * 1.05), "z", font=zf, fill=color)
        d.text((cx + r * 0.95, cy - r * 1.35), "Z", font=zf, fill=color)
    elif mood == "calm":
        d.arc(mouth, 20, 160, fill=color, width=6)
    else:
        d.line([cx - r * 0.3, cy + r * 0.35, cx + r * 0.3, cy + r * 0.35], fill=BLACK, width=6)


def render(state, size=DEFAULT_SIZE):
    """state: {"mood", "message", "session": {...}, "sensors": {...}, "study_mode"}"""
    w, h = size
    img = Image.new("RGB", size, WHITE)
    d = ImageDraw.Draw(img)
    mood = state.get("mood", "neutral")
    color = ACCENT.get(mood, BLACK)
    s = w / 800  # scale everything from the 800x480 design

    # Face
    face_r = int(130 * s)
    draw_face(d, int(170 * s), int(200 * s), face_r, mood, color)

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
        self.lock = threading.Lock()
        model = model or os.environ.get("EINK_MODEL")
        try:
            self.backend = _Waveshare(model, color) if model else _Inky()
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
            if not self.pending_force and time.time() - self.last_refresh < self.min_interval:
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

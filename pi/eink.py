"""Draws the companion screen and sends it to the e-ink display.

Defaults to our Waveshare 7.3" Spectra 6 panel (driver epd7in3e). Another
Waveshare panel can be chosen with --eink-model or EINK_MODEL, and
--eink-model inky uses a Pimoroni Inky instead.

Layout: a monochrome robot on the left, its current message on the right,
and a separate strip for session stats, room temperature and refresh time.

E-ink takes many seconds to refresh and flashes while it does, so refreshes
run in a background thread and are rate-limited (min_interval). Without an
display attached (e.g. on a laptop), it saves eink_preview.png instead,
so the layout can be checked anywhere.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import os
import threading
import time
from datetime import datetime
from functools import lru_cache
from math import cos, pi, sin

from PIL import Image, ImageDraw, ImageFont

PREVIEW_PATH = os.path.join(os.path.dirname(__file__), "eink_preview.png")
DEFAULT_MODEL = "epd7in3e"  # Waveshare 7.3" E Ink Spectra 6, 800x480
DEFAULT_SIZE = (800, 480)  # used for the preview; the real size is read from the display

BLACK, WHITE = (0, 0, 0), (255, 255, 255)
MOODS = ("neutral", "happy", "smug", "worried", "calm", "sleepy",
         "focused", "excited", "surprised", "sad", "annoyed", "playful")


@lru_cache(maxsize=32)
def _font(size, bold=False):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    dirs = ["/usr/share/fonts/truetype/dejavu", ""]
    for d in dirs:
        for n in names:
            try:
                return ImageFont.truetype(os.path.join(d, n) if d else n, size)
            except OSError:
                pass
    # Match the Pi's sans-serif styling when previewing on Windows.
    try:
        return ImageFont.truetype(
            os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts",
                         "arialbd.ttf" if bold else "arial.ttf"), size)
    except OSError:
        pass
    return ImageFont.load_default(size=size)


def draw_face(d, cx, cy, r, mood, color=BLACK):
    """A little robot with a dark visor and high-contrast expressions.

    Coordinates are relative to the head radius. Expressions use silhouette,
    not colour, so they work on both colour and monochrome e-paper.
    """
    def box(x0, y0, x1, y1):
        return tuple(round(v) for v in
                     (cx + x0 * r, cy + y0 * r, cx + x1 * r, cy + y1 * r))

    def rounded(bounds, radius, fill, outline=None, width=1):
        d.rounded_rectangle(box(*bounds), radius=max(1, round(radius * r)),
                            fill=fill, outline=outline, width=max(1, round(width * r)))

    def stroke(points, fill=WHITE, width=0.055):
        points = [(round(cx + x * r), round(cy + y * r)) for x, y in points]
        thickness = max(1, round(width * r))
        d.line(points, fill=fill, width=thickness, joint="curve")
        # Rounded ends make even the closed-eye expressions feel friendly.
        cap = thickness / 2
        for x, y in (points[0], points[-1]):
            d.ellipse((round(x - cap), round(y - cap),
                       round(x + cap), round(y + cap)), fill=fill)

    def curve(start, control, end, fill=WHITE, width=0.055):
        points = []
        for step in range(25):
            t = step / 24
            points.append(tuple((1 - t) ** 2 * start[i] +
                                2 * (1 - t) * t * control[i] + t ** 2 * end[i]
                                for i in (0, 1)))
        stroke(points, fill, width)

    def polygon(points, fill=WHITE):
        d.polygon([(round(cx + x * r), round(cy + y * r)) for x, y in points], fill=fill)

    # Antenna, side pods, and an inset visor in a softly rounded shell.
    stroke([(0, -0.69), (0, -0.97)], BLACK, 0.035)
    d.ellipse(box(-0.065, -1.07, 0.065, -0.94), fill=color, outline=BLACK,
              width=max(1, round(r * 0.02)))
    for side in (-1, 1):
        x0, x1 = sorted((side * 0.95, side * 1.13))
        rounded((x0, -0.18, x1, 0.30), 0.07, WHITE, BLACK, 0.035)
    rounded((-1, -0.72, 1, 0.81), 0.33, WHITE, BLACK, 0.035)
    rounded((-0.90, -0.61, 0.90, 0.64), 0.26, BLACK)
    rounded((-0.14, 0.705, 0.14, 0.73), 0.012, BLACK)

    if mood == "happy":
        for x in (-0.39, 0.39):
            curve((x - 0.16, -0.02), (x, -0.37), (x + 0.16, -0.02), width=0.075)
        d.chord(box(-0.23, 0.10, 0.23, 0.48), 0, 180, fill=WHITE)
    elif mood == "smug":
        for x, y in ((-0.39, -0.10), (0.39, -0.17)):
            rounded((x - 0.14, y, x + 0.14, y + 0.20), 0.06, WHITE)
            d.rectangle(box(x - 0.16, y - 0.01, x + 0.16, y + 0.045), fill=BLACK)
        curve((-0.13, 0.32), (0.12, 0.44), (0.29, 0.22), width=0.045)
    elif mood == "worried":
        for x in (-0.39, 0.39):
            rounded((x - 0.115, -0.11, x + 0.115, 0.16), 0.10, WHITE)
        stroke([(-0.55, -0.25), (-0.25, -0.34)], width=0.05)
        stroke([(0.25, -0.34), (0.55, -0.25)], width=0.05)
        curve((-0.16, 0.38), (0, 0.23), (0.16, 0.38), width=0.04)
    elif mood == "calm":
        for x in (-0.39, 0.39):
            curve((x - 0.16, -0.07), (x, 0.10), (x + 0.16, -0.07))
        curve((-0.13, 0.30), (0, 0.43), (0.13, 0.30), width=0.04)
    elif mood == "sleepy":
        stroke([(-0.55, 0.01), (-0.25, 0.06)])
        stroke([(0.25, 0.06), (0.55, 0.01)])
        d.ellipse(box(-0.08, 0.26, 0.08, 0.43), outline=WHITE,
                  width=max(1, round(r * 0.035)))
        # Drawn symbols avoid dependence on a font's special-character support.
        stroke([(0.58, -0.93), (0.75, -0.93), (0.58, -0.76), (0.75, -0.76)], BLACK, 0.025)
        stroke([(0.89, -1.08), (1.12, -1.08), (0.89, -0.85), (1.12, -0.85)], BLACK, 0.03)
    elif mood == "focused":
        # Narrow, inward-leaning eyes: locked in on the next fight.
        for side in (-1, 1):
            polygon([(side * 0.56, -0.22), (side * 0.22, -0.09),
                     (side * 0.22, 0.07), (side * 0.56, 0.07)])
        stroke([(-0.12, 0.34), (0.12, 0.34)], width=0.04)
    elif mood == "excited":
        # Five-point stars read clearly without relying on a symbol font.
        for x in (-0.39, 0.39):
            points = []
            for index in range(10):
                angle = -pi / 2 + index * pi / 5
                radius = 0.25 if index % 2 == 0 else 0.12
                points.append((x + cos(angle) * radius, -0.10 + sin(angle) * radius))
            polygon(points)
        d.chord(box(-0.25, 0.13, 0.25, 0.52), 0, 180, fill=WHITE)
    elif mood == "surprised":
        for x in (-0.39, 0.39):
            d.ellipse(box(x - 0.18, -0.31, x + 0.18, 0.08), fill=WHITE)
            d.ellipse(box(x - 0.08, -0.21, x + 0.08, -0.02), fill=BLACK)
        d.ellipse(box(-0.12, 0.22, 0.12, 0.48), outline=WHITE,
                  width=max(1, round(r * 0.05)))
    elif mood == "sad":
        for x in (-0.39, 0.39):
            curve((x - 0.15, -0.06), (x, 0.13), (x + 0.15, -0.06), width=0.05)
        stroke([(-0.55, -0.24), (-0.29, -0.33)], width=0.04)
        stroke([(0.29, -0.33), (0.55, -0.24)], width=0.04)
        curve((-0.13, 0.38), (0, 0.22), (0.13, 0.38), width=0.04)
        # A single solid tear, kept inside the visor.
        polygon([(0.64, 0.07), (0.58, 0.20), (0.70, 0.20)])
        d.ellipse(box(0.58, 0.15, 0.70, 0.27), fill=WHITE)
    elif mood == "annoyed":
        for x in (-0.39, 0.39):
            rounded((x - 0.16, -0.09, x + 0.16, 0.09), 0.04, WHITE)
            # Both pupils look sideways for an exaggerated eye roll.
            rounded((x + 0.015, -0.10, x + 0.085, 0.06), 0.02, BLACK)
        stroke([(-0.56, -0.29), (-0.23, -0.19)], width=0.04)
        stroke([(0.23, -0.19), (0.56, -0.29)], width=0.04)
        stroke([(-0.16, 0.35), (0.03, 0.30), (0.16, 0.35)], width=0.04)
    elif mood == "playful":
        rounded((-0.52, -0.30, -0.26, 0.15), 0.12, WHITE)
        stroke([(0.24, -0.12), (0.47, -0.02), (0.24, 0.08)], width=0.06)
        curve((-0.21, 0.27), (0, 0.46), (0.24, 0.24), width=0.045)
        rounded((0.015, 0.34, 0.17, 0.50), 0.065, WHITE)
        stroke([(0.09, 0.38), (0.09, 0.44)], BLACK, 0.018)
    else:
        for x in (-0.39, 0.39):
            rounded((x - 0.13, -0.30, x + 0.13, 0.15), 0.12, WHITE)
        curve((-0.12, 0.32), (0, 0.40), (0.12, 0.32), width=0.04)


def _wrap_text(d, text, font, max_width):
    """Wrap by rendered width, including unbroken words from an LLM recap."""
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if d.textlength(candidate, font=font) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        for char in word:
            if line and d.textlength(line + char, font=font) > max_width:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines


def _draw_message(d, message):
    """Fit the message above the stats, with an ellipsis only if necessary."""
    max_width, max_height = 376, 192
    for font_size in (38, 36, 34, 32, 30, 28, 26, 24):
        font = _font(font_size, bold=True)
        lines = _wrap_text(d, message, font, max_width)
        line_height = font_size + 8
        if len(lines) * line_height <= max_height:
            break
    capacity = max_height // line_height
    if len(lines) > capacity:
        lines = lines[:capacity]
        while lines[-1] and d.textlength(lines[-1] + "...", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "..."
    for index, line in enumerate(lines):
        d.text((384, 176 + index * line_height), line, font=font, fill=BLACK, anchor="lt")


def face_png(mood, size=240):
    """Just the robot, as PNG bytes, for the web dashboard."""
    import io
    if mood not in MOODS:
        mood = "neutral"
    img = Image.new("RGB", (size, size), WHITE)
    draw_face(ImageDraw.Draw(img), size / 2, size * 0.56, size * 0.40, mood)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def render(state, size=DEFAULT_SIZE):
    """state: {"mood", "message", "session": {...}, "sensors": {...}, "study_mode"}"""
    # Compose at the panel's native size, then fit other panels without clipping
    # or stretching the robot when their aspect ratio differs.
    img = Image.new("RGB", DEFAULT_SIZE, WHITE)
    d = ImageDraw.Draw(img)
    mood = state.get("mood", "neutral")
    if mood not in MOODS:
        mood = "neutral"

    # A small identity mark, generous margins, and one clear mode indicator.
    for x in (32, 44):
        d.rounded_rectangle((x, 31, x + 6, 45), radius=3, fill=BLACK)
    d.text((64, 30), "DESK COMPANION", font=_font(18, bold=True), fill=BLACK, anchor="lt")
    mode = "STUDY" if state.get("study_mode") else ("IN MATCH" if state.get("in_match") else "LOBBY")
    mode_font = _font(15, bold=True)
    mode_width = round(d.textlength(mode, font=mode_font)) + 32
    d.rounded_rectangle((768 - mode_width, 22, 768, 55), radius=16, outline=BLACK, width=2)
    d.text((768 - mode_width / 2, 38), mode, font=mode_font, fill=BLACK, anchor="mm")
    d.line((32, 76, 768, 76), fill=BLACK, width=2)

    draw_face(d, 190, 235, 132, mood)
    d.text((384, 127), mood.upper(), font=_font(16, bold=True), fill=BLACK, anchor="lt")
    d.line((384, 154, 422, 154), fill=BLACK, width=3)
    _draw_message(d, state.get("message", ""))

    # Each value has its own space; long messages cannot collide with this strip.
    d.line((32, 390, 768, 390), fill=BLACK, width=2)
    sess = state.get("session", {})
    sensors = state.get("sensors", {})
    temp = sensors.get("temp_c")
    temp_str = f"{temp * 9 / 5 + 32:.0f}\N{DEGREE SIGN}F" if isinstance(temp, (int, float)) else "--"
    clock = datetime.now().strftime("%I:%M %p").lstrip("0")
    items = ((32, "GAMES", str(sess.get("games", 0))),
             (164, "WINS", str(sess.get("wins", 0))),
             (296, "ELIMS", str(sess.get("elims", 0))),
             (468, "ROOM", temp_str), (630, "UPDATED", clock))
    d.line((436, 410, 436, 457), fill=BLACK, width=2)
    for x, label, value in items:
        d.text((x, 409), label, font=_font(13, bold=True), fill=BLACK, anchor="lt")
        font_size = 24 if label == "UPDATED" else 28
        while font_size > 12 and d.textlength(value, font=_font(font_size, bold=True)) > 128:
            font_size -= 2
        d.text((x, 432), value, font=_font(font_size, bold=True), fill=BLACK, anchor="lt")

    if size == DEFAULT_SIZE:
        return img
    w, h = size
    scale = min(w / DEFAULT_SIZE[0], h / DEFAULT_SIZE[1])
    fitted = img.resize((max(1, round(800 * scale)), max(1, round(480 * scale))),
                        Image.Resampling.LANCZOS)
    output = Image.new("RGB", size, WHITE)
    output.paste(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
    return output


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
                self._schedule_refresh(wait)
                return
            self.busy = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _schedule_refresh(self, wait):
        """Schedule pending work while holding self.lock."""
        if self.timer is None:
            self.timer = threading.Timer(max(0, wait), self._wake)
            self.timer.daemon = True
            self.timer.start()

    def _wake(self):
        with self.lock:
            self.timer = None
            if self.busy or self.pending is None:
                return
            # A forced update may have happened since this timer was created.
            wait = self.min_interval - (time.time() - self.last_refresh)
            if not self.pending_force and wait > 0:
                self._schedule_refresh(wait)
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
                    if self.pending is not None:
                        # Soft reactions can arrive while the panel is busy.
                        self._schedule_refresh(self.min_interval - (time.time() - self.last_refresh))
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

"""Preview the robot display without connecting to MQTT or physical hardware."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from eink import BLACK, MOODS, PREVIEW_PATH, WHITE, _font, draw_face, render


MESSAGES = {
    "neutral": "Hey. Let's play.",
    "happy": "Crown secured. Insufferable now.",
    "smug": "We don't talk about that one.",
    "worried": "Breathe. The desk didn't do anything.",
    "calm": "Study mode. I'll be quiet. Mostly.",
    "sleepy": "It's 2:14 AM. Your bed misses you.",
    "focused": "Locked in. Let's make this one count.",
    "excited": "Okay, that was clean. Do it again.",
    "surprised": "Minor setback. Major crawling energy.",
    "sad": "That one hurt. Next bus, fresh start.",
    "annoyed": "The desk has filed a complaint.",
    "playful": "Boop. Your emotional support robot is here.",
}


def sample_state(mood, message=None):
    return {
        "mood": mood,
        "message": MESSAGES[mood] if message is None else message,
        "session": {"games": 8, "wins": 2, "elims": 24},
        "sensors": {"temp_c": 22.5},
        "study_mode": mood == "calm",
        "in_match": mood in ("worried", "focused", "excited", "surprised", "annoyed"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mood", choices=MOODS, default="neutral")
    parser.add_argument("--message", help="try a custom recap or reaction")
    layout = parser.add_mutually_exclusive_group()
    layout.add_argument("--sheet", action="store_true", help="show every full screen together")
    layout.add_argument("--faces", action="store_true", help="show a compact gallery of expressions")
    parser.add_argument("--output", type=Path, default=Path(PREVIEW_PATH))
    args = parser.parse_args()

    if args.sheet:
        # Native-sized screens, with a narrow gutter that is not part of the UI.
        rows = (len(MOODS) + 1) // 2
        image = Image.new("RGB", (1648, 16 + rows * 496), (225, 225, 225))
        for index, mood in enumerate(MOODS):
            tile = render(sample_state(mood, args.message))
            image.paste(tile, (16 + (index % 2) * 816, 16 + (index // 2) * 496))
    elif args.faces:
        columns, tile_w, tile_h, gutter = 4, 280, 292, 12
        rows = (len(MOODS) + columns - 1) // columns
        image = Image.new("RGB", (gutter + columns * (tile_w + gutter),
                                  gutter + rows * (tile_h + gutter)), (225, 225, 225))
        for index, mood in enumerate(MOODS):
            tile = Image.new("RGB", (tile_w, tile_h), WHITE)
            d = ImageDraw.Draw(tile)
            draw_face(d, tile_w // 2, 145, 104, mood)
            d.text((tile_w // 2, 257), mood.upper(), font=_font(16, bold=True),
                   fill=BLACK, anchor="mt")
            image.paste(tile, (gutter + (index % columns) * (tile_w + gutter),
                               gutter + (index // columns) * (tile_h + gutter)))
    else:
        image = render(sample_state(args.mood, args.message))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(args.output.resolve())


if __name__ == "__main__":
    main()

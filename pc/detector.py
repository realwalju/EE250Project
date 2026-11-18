"""Screen-capture event detector for the gaming PC (work in progress).

Captures only small regions of the screen (like OBS does - it never touches
the game process), runs detection on each region, and publishes confirmed
events to the Pi over MQTT.

Done:  capture loop, region crops, multi-frame confirmation, MQTT publishing,
       --save-crops to collect sample images for tuning.
To do: the actual detection functions (color thresholds / template matching /
       OCR), tuned on recorded gameplay at our resolution and UI scale.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import argparse
import json
import os
import time

import cv2
import mss
import numpy as np
import paho.mqtt.client as mqtt

T_GAME = "desk/game/event"

# Regions as fractions of the screen (left, top, width, height).
# Placeholder values for a 1920x1080 HUD - adjust with --save-crops.
REGIONS = {
    "center_banner": (0.30, 0.15, 0.40, 0.12),  # "Victory Royale", "Knocked", elim messages
    "elim_feed":     (0.70, 0.55, 0.28, 0.15),  # recent eliminations text
    "health_bar":    (0.36, 0.89, 0.14, 0.03),  # green health bar
}


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


def to_pixels(region, monitor):
    left, top, width, height = region
    return {
        "left": monitor["left"] + int(left * monitor["width"]),
        "top": monitor["top"] + int(top * monitor["height"]),
        "width": int(width * monitor["width"]),
        "height": int(height * monitor["height"]),
    }


# ---------- Detection (TODO: tune on real gameplay) ----------
def detect_victory(banner_bgr):
    # TODO: template-match a saved "Victory Royale" crop, e.g.
    # cv2.matchTemplate(banner_gray, template, cv2.TM_CCOEFF_NORMED).max() > 0.7
    return False


def detect_knocked(banner_bgr):
    # TODO: template-match / OCR the "knocked" banner
    return False


def detect_elim(feed_bgr):
    # TODO: OCR the elimination feed for our username, or match the
    # "You eliminated" message
    return False


def health_fraction(bar_bgr):
    """Rough health estimate: fraction of the bar that is green."""
    hsv = cv2.cvtColor(bar_bgr, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (40, 80, 80), (85, 255, 255))
    return float(np.count_nonzero(green)) / green.size


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--save-crops", action="store_true",
                        help="save region crops to recordings/ for tuning")
    args = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="desk-detector")
    client.connect(args.broker)
    client.loop_start()

    confirm = ConfirmFilter(frames=3, cooldown=3.0)
    if args.save_crops:
        os.makedirs("recordings", exist_ok=True)

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary screen
        boxes = {name: to_pixels(r, monitor) for name, r in REGIONS.items()}
        frame_no = 0
        while True:
            start = time.time()
            crops = {name: cv2.cvtColor(np.array(sct.grab(box)), cv2.COLOR_BGRA2BGR)
                     for name, box in boxes.items()}

            seen = set()
            if detect_victory(crops["center_banner"]):
                seen.add("victory")
            if detect_knocked(crops["center_banner"]):
                seen.add("knocked")
            if detect_elim(crops["elim_feed"]):
                seen.add("elim")

            for event in confirm.update(seen, start):
                client.publish(T_GAME, json.dumps({"type": event}))
                print(f"event: {event}")

            if args.save_crops and frame_no % 10 == 0:
                for name, img in crops.items():
                    cv2.imwrite(f"recordings/{frame_no:06d}_{name}.png", img)
            frame_no += 1

            time.sleep(max(0.0, 1.0 / args.fps - (time.time() - start)))


if __name__ == "__main__":
    main()

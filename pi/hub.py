"""Central hub for the Fortnite Desk Companion (runs on the Raspberry Pi 5).

Subscribes to every device over MQTT, tracks match state, detects tilt by
combining game events with desk vibration, tells the mood light which effect
to play, and logs everything to SQLite.

Still to come: LLM recaps, e-ink companion, Flask dashboard, OBS overlay.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import argparse
import json
import sqlite3
import time
from collections import deque

import paho.mqtt.client as mqtt

# ---------- MQTT topics ----------
T_GAME = "desk/game/event"
T_ACTIVITY = "desk/pc/activity"
T_EFFECT = "desk/leds/effect"
T_BUTTON = "desk/input/button"
T_SENSORS = "desk/sensors"
T_NOTIFY = "desk/notify"

# Game event type -> one-shot mood light effect
GAME_EFFECTS = {
    "elim": "elim",
    "knocked": "knocked",
    "eliminated": "eliminated",
    "victory": "victory",
}
MATCH_END_EVENTS = {"eliminated", "victory", "match_end"}


class TiltDetector:
    """Tilt = a bad game event (knocked/eliminated) followed closely by a burst
    of desk vibration. Neither signal alone counts.

    - event_window: how long after a bad event slams still count (s)
    - vib_window:   sliding window for summing vibration pulses (s)
    - vib_threshold: pulses in vib_window needed to count as slamming
    - cooldown:     minimum time between tilt alerts (s)
    """

    def __init__(self, event_window=10.0, vib_window=5.0, vib_threshold=6, cooldown=30.0):
        self.event_window = event_window
        self.vib_window = vib_window
        self.vib_threshold = vib_threshold
        self.cooldown = cooldown
        self.vib = deque()  # (timestamp, pulse_count)
        self.last_bad_event = None
        self.last_tilt = None

    def on_game_event(self, etype, now):
        if etype in ("knocked", "eliminated"):
            self.last_bad_event = now
        return self._check(now)

    def on_vibration(self, count, now):
        if count > 0:
            self.vib.append((now, count))
        return self._check(now)

    def vib_total(self, now):
        while self.vib and now - self.vib[0][0] > self.vib_window:
            self.vib.popleft()
        return sum(c for _, c in self.vib)

    def _check(self, now):
        if self.last_bad_event is None or now - self.last_bad_event > self.event_window:
            return False
        if self.last_tilt is not None and now - self.last_tilt < self.cooldown:
            return False
        if self.vib_total(now) >= self.vib_threshold:
            self.last_tilt = now
            return True
        return False


class MatchState:
    """Tracks lobby -> in match -> ended, and per-match stats."""

    def __init__(self):
        self.in_match = False
        self.match_id = 0
        self.stats = {}

    def on_event(self, etype, now):
        """Returns a match summary dict when a match ends, otherwise None."""
        if etype == "match_start" or (not self.in_match and etype in GAME_EFFECTS):
            self._start(now)
        if not self.in_match:
            return None

        if etype == "elim":
            self.stats["elims"] += 1
        elif etype == "knocked":
            self.stats["knocks"] += 1
        self.stats["timeline"].append({"t": round(now - self.stats["start"], 1), "type": etype})

        if etype in MATCH_END_EVENTS:
            self.in_match = False
            summary = dict(self.stats)
            summary["result"] = "win" if etype == "victory" else "loss" if etype == "eliminated" else "ended"
            summary["duration_s"] = round(now - self.stats["start"], 1)
            return summary
        return None

    def _start(self, now):
        self.in_match = True
        self.match_id += 1
        self.stats = {"match_id": self.match_id, "start": now, "elims": 0, "knocks": 0, "timeline": []}


class Hub:
    def __init__(self, publish, db_path="desk.db"):
        self.publish = publish  # publish(topic, dict) - injected so logic is testable
        self.match = MatchState()
        self.tilt = TiltDetector()
        self.study_mode = False
        self.latest_sensors = {}
        self.latest_activity = {}
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events  (ts REAL, topic TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS matches (ts REAL, match_id INTEGER, summary TEXT);
            CREATE TABLE IF NOT EXISTS clips   (ts REAL, match_id INTEGER);
            """
        )
        self.db.commit()

    # ---------- outputs ----------
    def effect(self, name):
        self.publish(T_EFFECT, {"effect": name})
        print(f"  -> light: {name}")

    def base_effect(self):
        if self.study_mode:
            return "study"
        return "match" if self.match.in_match else "lobby"

    # ---------- inputs ----------
    def handle(self, topic, data, now=None):
        now = time.time() if now is None else now
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (now, topic, json.dumps(data)))
        self.db.commit()

        if topic == T_GAME:
            self.on_game(data, now)
        elif topic == T_SENSORS:
            self.on_sensors(data, now)
        elif topic == T_BUTTON:
            self.on_button(data, now)
        elif topic == T_ACTIVITY:
            self.latest_activity = data

    def on_game(self, data, now):
        etype = data.get("type")
        print(f"game: {etype}")
        was_in_match = self.match.in_match
        summary = self.match.on_event(etype, now)

        if self.match.in_match and not was_in_match:
            self.study_mode = False
            self.effect("match")
        if etype in GAME_EFFECTS:
            self.effect(GAME_EFFECTS[etype])
        if self.tilt.on_game_event(etype, now):
            self.on_tilt()
        if summary:
            self.on_match_end(summary, now)

    def on_sensors(self, data, now):
        self.latest_sensors = data
        if self.tilt.on_vibration(int(data.get("vib") or 0), now):
            self.on_tilt()

    def on_button(self, data, now):
        button_id = data.get("id")
        print(f"button: {button_id}")
        if button_id == "clip":
            self.db.execute("INSERT INTO clips VALUES (?, ?)", (now, self.match.match_id))
            self.db.commit()
            print("  clip saved")
        elif button_id == "interact":
            self.publish(T_NOTIFY, {"msg": 0})
            # TODO: show the companion's latest message / recap on the e-ink
        elif button_id == "mode":
            self.study_mode = not self.study_mode
            print(f"  study mode {'on' if self.study_mode else 'off'}")
            self.effect(self.base_effect())

    def on_tilt(self):
        print("TILT detected (bad event + desk slams)")
        self.effect("tilt")
        # TODO: companion reacts on e-ink / overlay

    def on_match_end(self, summary, now):
        print(f"match {summary['match_id']} ended: {summary['result']}, "
              f"{summary['elims']} elims, {summary['duration_s']}s")
        self.db.execute("INSERT INTO matches VALUES (?, ?, ?)",
                        (now, summary["match_id"], json.dumps(summary)))
        self.db.commit()
        self.effect(self.base_effect())  # mood light returns here after the one-shot effect
        self.publish(T_NOTIFY, {"msg": 1})
        # TODO: send summary to the LLM for a recap and draw it on the e-ink


def main():
    parser = argparse.ArgumentParser(description="Desk companion hub")
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--db", default="desk.db")
    args = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="desk-hub")

    def publish(topic, payload):
        client.publish(topic, json.dumps(payload))

    hub = Hub(publish, args.db)

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connected to {args.broker} ({reason_code})")
        client.subscribe([(T_GAME, 0), (T_ACTIVITY, 0), (T_BUTTON, 0), (T_SENSORS, 0)])
        hub.effect(hub.base_effect())

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            print(f"bad payload on {msg.topic}: {msg.payload!r}")
            return
        hub.handle(msg.topic, data)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect(args.broker, args.port)
    client.loop_forever()


if __name__ == "__main__":
    main()

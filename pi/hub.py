"""Central hub for the Fortnite Desk Companion (runs on the Raspberry Pi 5).

Subscribes to every device over MQTT, tracks match state, detects tilt by
combining game events with desk vibration, tells the mood light which effect
to play, logs everything to SQLite, drives the e-ink companion, and serves the
web dashboard (see web.py).

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import argparse
import json
import sqlite3
import threading
import time
from collections import deque

import paho.mqtt.client as mqtt

from companion import Companion, load_env

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

    def vib_total(self, now, since=None):
        """Pulses in the sliding window (optionally only those at or after `since`)."""
        while self.vib and now - self.vib[0][0] > self.vib_window:
            self.vib.popleft()
        return sum(c for ts, c in self.vib if since is None or ts >= since)

    def _check(self, now):
        if self.last_bad_event is None or now - self.last_bad_event > self.event_window:
            return False
        if self.last_tilt is not None and now - self.last_tilt < self.cooldown:
            return False
        # Only slams AFTER the bad event count - that's what makes it a reaction.
        if self.vib_total(now, since=self.last_bad_event) >= self.vib_threshold:
            self.last_tilt = now
            return True
        return False


class MatchState:
    """Tracks lobby -> playing -> dead/ended, and per-match stats.

    Dying doesn't end the match for good: in squads you spectate your team and
    can be rebooted. While "dead":
      - another "eliminated" (a teammate's death screen, the squad's
        "You placed #N" screen) is ignored instead of opening a fake new match
      - a kill or knock means you were rebooted: the same match continues
      - "victory" means your team won: the match result becomes a win
    A new bus (match_start) or REBOOT_WINDOW without anything ends it.
    """

    SAME_BUS = 120          # s: the bus prompt can be detected more than once on the same bus
    REBOOT_WINDOW = 20 * 60  # s: after this long dead, the match is over for sure

    def __init__(self, last_match_id=0):
        self.state = "lobby"   # "lobby" | "playing" | "dead"
        self.match_id = last_match_id
        self.stats = {}
        self.dead_since = None
        self.ignored = False   # True if the last event didn't count (see Hub.on_game)

    @property
    def in_match(self):
        return self.state == "playing"

    def on_event(self, etype, now):
        """Returns a match summary dict when a match ends (or its result
        changes after a reboot / team win), otherwise None."""
        self.ignored = False
        if self.state == "dead" and now - self.dead_since > self.REBOOT_WINDOW:
            self.state = "lobby"

        if etype == "match_start":
            if self.state != "lobby" and now - self.stats["start"] < self.SAME_BUS:
                self.ignored = True
                return None
            self._start(now)
            return None

        if self.state == "lobby":
            if etype not in ("elim", "knocked"):
                # a death/victory screen with no match running (e.g. spectating
                # after the match already ended) - don't invent a 0-kill match
                self.ignored = True
                return None
            self._start(now)  # hub started mid-match: count from here

        if self.state == "dead":
            if etype in ("elim", "knocked"):
                self.state = "playing"  # rebooted
                self.stats["reboots"] += 1
                self._log("reboot", now)
            elif etype == "victory":
                self.state = "lobby"
                self._log("victory", now)
                return self._summary("win", now)  # your squad won after you died
            else:
                self.ignored = True
                return None

        if etype == "elim":
            self.stats["elims"] += 1
        elif etype == "knocked":
            self.stats["knocks"] += 1
        self._log(etype, now)

        if etype == "eliminated":
            self.state = "dead"
            self.dead_since = now
            return self._summary("loss", now)
        if etype in MATCH_END_EVENTS:
            self.state = "lobby"
            return self._summary("win" if etype == "victory" else "ended", now)
        return None

    def _log(self, etype, now):
        self.stats["timeline"].append({"t": round(now - self.stats["start"], 1), "type": etype})

    def _summary(self, result, now):
        summary = dict(self.stats, timeline=list(self.stats["timeline"]))
        summary["result"] = result
        summary["duration_s"] = round(now - self.stats["start"], 1)
        return summary

    def _start(self, now):
        self.state = "playing"
        self.match_id += 1
        self.dead_since = None
        self.stats = {"match_id": self.match_id, "start": now, "elims": 0, "knocks": 0,
                      "reboots": 0, "timeline": []}


class Hub:
    def __init__(self, publish, db_path="desk.db", display=None):
        self.publish = publish  # publish(topic, dict) - injected so logic is testable
        self.display = display  # EinkDisplay or None
        self.lock = threading.RLock()  # MQTT, Flask and recap threads all touch hub state
        self.tilt = TiltDetector()
        self.study_mode = False
        self.latest_sensors = {}
        self.latest_activity = {}
        self.feed = deque(maxlen=500)  # recent events for the dashboard
        self.feed_id = 0
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        # keep counting match ids from the database, so a hub restart doesn't
        # reuse an old id (a rebooted match replaces its earlier summary by id)
        last_id = self.db.execute("SELECT MAX(match_id) FROM matches").fetchone()[0] or 0
        self.match = MatchState(last_id)
        self.companion = Companion(on_update=self._companion_changed)

    def _init_db(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events  (ts REAL, topic TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS matches (ts REAL, match_id INTEGER, summary TEXT);
            CREATE TABLE IF NOT EXISTS clips   (ts REAL, match_id INTEGER);
            """
        )
        self.db.commit()

    # ---------- feed for web pages ----------
    def add_feed(self, kind, data):
        with self.lock:
            self.feed_id += 1
            self.feed.append({"id": self.feed_id, "ts": time.time(), "kind": kind, "data": data})

    def feed_since(self, since):
        with self.lock:
            events = [e for e in self.feed if e["id"] > since][-50:]
            return {"last_id": self.feed_id, "events": events}

    def web_state(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT summary FROM matches ORDER BY ts DESC LIMIT 10").fetchall()
            matches = [json.loads(r[0]) for r in rows]
            for m in matches:
                m.pop("timeline", None)
            return {
                "in_match": self.match.in_match,
                "study_mode": self.study_mode,
                "match": ({k: self.match.stats[k] for k in ("match_id", "elims", "knocks")}
                          if self.match.in_match else None),
                "sensors": self.latest_sensors,
                "activity": self.latest_activity,
                "vib_recent": self.tilt.vib_total(time.time()),
                "companion": self.companion.snapshot(),
                "matches": matches,
                "last_id": self.feed_id,
            }

    def eink_state(self):
        with self.lock:
            state = self.companion.snapshot()
            state.update(sensors=dict(self.latest_sensors), study_mode=self.study_mode,
                         in_match=self.match.in_match)
            return state

    def refresh_eink(self, force=False):
        if self.display:
            self.display.request(self.eink_state(), force=force)

    def _companion_changed(self, urgent=True):
        snap = self.companion.snapshot()
        self.add_feed("companion", {"mood": snap["mood"], "message": snap["message"]})
        print(f"  companion ({snap['mood']}): {snap['message']}")
        # Urgent (recaps, tilt, wins) refresh right away; in-match reactions
        # wait for the display's rate limit so it doesn't flash nonstop.
        self.refresh_eink(force=urgent)

    # ---------- outputs ----------
    def effect(self, name):
        self.publish(T_EFFECT, {"effect": name})
        self.add_feed("effect", {"effect": name})
        print(f"  -> light: {name}")

    def base_effect(self):
        if self.study_mode:
            return "study"
        return "match" if self.match.in_match else "lobby"

    # ---------- inputs ----------
    def handle(self, topic, data, now=None):
        now = time.time() if now is None else now
        with self.lock:
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
        self.add_feed("game", data)
        was_in_match = self.match.in_match
        summary = self.match.on_event(etype, now)
        if self.match.ignored:
            # e.g. a teammate's death screen while you spectate, or the bus
            # prompt seen again: logged, but no light, reaction or new match
            print(f"  (ignored: {etype} while {self.match.state})")
            return

        if self.match.in_match and not was_in_match:
            self.study_mode = False
            self.effect("match")
        if etype in GAME_EFFECTS:
            self.effect(GAME_EFFECTS[etype])
        if self.match.in_match:
            self.companion.on_game_event(etype, now)
        if self.tilt.on_game_event(etype, now):
            self.on_tilt()
        if summary:
            self.on_match_end(summary, now)

    def on_sensors(self, data, now):
        self.latest_sensors = data
        if self.tilt.on_vibration(int(data.get("vib") or 0), now):
            self.on_tilt()
        if not self.match.in_match:
            self.companion.check_late_night()

    def on_button(self, data, now):
        button_id = data.get("id")
        print(f"button: {button_id}")
        self.add_feed("button", data)
        if button_id == "clip":
            self.db.execute("INSERT INTO clips VALUES (?, ?)", (now, self.match.match_id))
            self.db.commit()
            print("  clip saved")
            self.effect("clip")
            self.companion.react("clip")
        elif button_id == "interact":
            self.publish(T_NOTIFY, {"msg": 0})  # stop the LED's "new message" blink
            self.effect("ping")
            self.companion.react("interact")
        elif button_id == "mode":
            self.study_mode = not self.study_mode
            print(f"  study mode {'on' if self.study_mode else 'off'}")
            self.effect(self.base_effect())
            if self.study_mode:
                self.companion.react("study")

    def on_tilt(self):
        print("TILT detected (bad event + desk slams)")
        self.effect("tilt")
        self.companion.react("tilt")

    def on_match_end(self, summary, now):
        print(f"match {summary['match_id']} result: {summary['result']}, "
              f"{summary['elims']} elims, {summary['duration_s']}s")
        # a reboot or a squad win after you died updates the same match
        self.db.execute("DELETE FROM matches WHERE match_id = ?", (summary["match_id"],))
        self.db.execute("INSERT INTO matches VALUES (?, ?, ?)",
                        (now, summary["match_id"], json.dumps(summary)))
        self.db.commit()
        self.effect(self.base_effect())  # mood light returns here after the one-shot effect
        self.publish(T_NOTIFY, {"msg": 1})  # sensor board LED double-blinks
        self.companion.on_match_end(summary)  # quick reaction now, LLM recap in background


def main():
    parser = argparse.ArgumentParser(description="Desk companion hub")
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--db", default="desk.db")
    parser.add_argument("--web-port", type=int, default=5000)
    parser.add_argument("--no-eink", action="store_true", help="don't drive the e-ink display")
    parser.add_argument("--eink-model", help="Waveshare driver name (default epd7in3e, our 7.3\" Spectra 6), or inky")
    parser.add_argument("--eink-color", choices=["auto", "mono", "color"], default="auto",
                        help="force black/white or colour output for a Waveshare panel")
    args = parser.parse_args()
    load_env()  # pi/.env: API key, EINK_MODEL

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="desk-hub")

    def publish(topic, payload):
        client.publish(topic, json.dumps(payload))

    display = None
    if not args.no_eink:
        from eink import EinkDisplay
        color = {"auto": None, "mono": False, "color": True}[args.eink_color]
        display = EinkDisplay(min_interval=20, model=args.eink_model, color=color)
    hub = Hub(publish, args.db, display=display)

    # Web dashboard in a background thread
    from web import create_app
    app = create_app(hub)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=args.web_port, threaded=True),
                     daemon=True).start()
    print(f"dashboard: http://<this Pi's IP>:{args.web_port}/")

    # Refresh the e-ink every 10 minutes so the clock and stats stay current
    def eink_timer():
        while True:
            hub.refresh_eink(force=True)
            time.sleep(600)
    if display:
        threading.Thread(target=eink_timer, daemon=True).start()

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

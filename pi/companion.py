"""The desk companion's brain: mood, quick reactions and LLM recaps.

Quick reactions (tilt, wins, late nights) are templates so they appear
instantly. Post-match recaps go to the LLM in a background thread, so a slow
or failed API call never blocks the hub. Without an API key, recaps fall back
to a template.

Set ANTHROPIC_API_KEY in pi/.env (gitignored) to enable LLM recaps.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import os
import random
import threading
import time
from datetime import datetime

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")

PERSONALITY = (
    "You are a small, sarcastic but supportive desk companion who lives on an "
    "e-ink screen next to a Fortnite player. You speak in short, punchy lines "
    "like a friend roasting them on stream. Never use emoji."
)

REACTIONS = {
    "tilt": ["Breathe. The desk didn't do anything.", "Easy. That desk is innocent.",
             "Tilt detected. Maybe hydrate?"],
    "win": ["VICTORY ROYALE. Who even are you?", "A win! I'm telling everyone.",
            "Crown secured. Insufferable now."],
    "loss": ["Back to the bus.", "We don't talk about that one.", "Shake it off. Requeue."],
    "study": ["Study mode. I'll be quiet. Mostly.", "Books out. Fortnite can wait."],
    "late": ["It's {time}. Your bed misses you.", "{time} and the lights are off. Sleep?"],
}


def load_env(path=os.path.join(os.path.dirname(__file__), ".env")):
    """Tiny .env loader so we don't need python-dotenv."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


class Companion:
    def __init__(self, on_update=None):
        load_env()
        self.on_update = on_update or (lambda: None)  # called whenever the message changes
        self.mood = "neutral"
        self.message = "Hey. Let's play."
        self.last_recap = None
        self.session = {"games": 0, "wins": 0, "elims": 0, "tilts": 0}
        self._client = None
        self._lock = threading.Lock()

    # ---------- state ----------
    def snapshot(self):
        with self._lock:
            return {"mood": self.mood, "message": self.message,
                    "last_recap": self.last_recap, "session": dict(self.session)}

    def _say(self, mood, message):
        with self._lock:
            self.mood, self.message = mood, message
        self.on_update()

    # ---------- quick reactions (instant, no LLM) ----------
    def react(self, kind, **fmt):
        mood = {"tilt": "worried", "win": "happy", "loss": "smug",
                "study": "calm", "late": "sleepy"}.get(kind, "neutral")
        if kind == "tilt":
            with self._lock:
                self.session["tilts"] += 1
        self._say(mood, random.choice(REACTIONS[kind]).format(**fmt))

    def check_late_night(self, light_pct, now=None):
        """Lights off after midnight -> sleepy nudge. Returns True if it reacted."""
        now = now or datetime.now()
        if 0 <= now.hour < 5 and light_pct is not None and light_pct < 10 and self.mood != "sleepy":
            self.react("late", time=now.strftime("%I:%M %p").lstrip("0"))
            return True
        return False

    # ---------- post-match ----------
    def on_match_end(self, summary):
        with self._lock:
            self.session["games"] += 1
            self.session["elims"] += summary["elims"]
            if summary["result"] == "win":
                self.session["wins"] += 1
        self.react("win" if summary["result"] == "win" else "loss")
        threading.Thread(target=self._recap, args=(summary,), daemon=True).start()

    def _recap(self, summary):
        text = self._llm_recap(summary) or self._template_recap(summary)
        with self._lock:
            self.last_recap = {"match_id": summary["match_id"], "text": text, "ts": time.time()}
        self._say(self.mood, text)

    def _template_recap(self, s):
        minutes = s["duration_s"] / 60
        verdict = "Won it." if s["result"] == "win" else "Didn't make it."
        return f"{verdict} {s['elims']} elims, knocked {s['knocks']}x in {minutes:.1f} min."

    def _llm_recap(self, s):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            if self._client is None:
                import anthropic
                self._client = anthropic.Anthropic(timeout=20.0)
            with self._lock:
                session = dict(self.session)
            timeline = ", ".join(f"{e['t']:.0f}s {e['type']}" for e in s["timeline"])
            prompt = (
                f"Match result: {s['result']}. Eliminations: {s['elims']}. "
                f"Times knocked: {s['knocks']}. Duration: {s['duration_s']:.0f}s.\n"
                f"Timeline: {timeline}\n"
                f"Session so far: {session['games']} games, {session['wins']} wins, "
                f"{session['tilts']} tilts.\n\n"
                "Write a recap for the e-ink screen: at most 2 short sentences, under "
                "140 characters total, with one concrete tip based on the timeline."
            )
            resp = self._client.messages.create(
                model=MODEL, max_tokens=120, system=PERSONALITY,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:  # network down, bad key, etc. -> template instead
            print(f"LLM recap failed: {e}")
            return None

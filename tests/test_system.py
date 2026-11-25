"""Automated tests for the parts that don't need hardware.

Run from the repo root:  python -m pytest tests -q

Covers: tilt detection, match state, the hub's light effects for a full
fake match, the web API, the detector's multi-frame filter, and template
matching on synthetic game frames.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import os
import sys
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "pc"))

from hub import Hub, MatchState, TiltDetector, T_BUTTON, T_EFFECT, T_NOTIFY, T_SENSORS  # noqa: E402
from test_publisher import DEMO_SCRIPT  # noqa: E402


# ---------- hub logic ----------
def make_hub():
    sent = []
    hub = Hub(lambda topic, payload: sent.append((topic, payload)), ":memory:")
    return hub, sent


def effects(sent):
    return [p["effect"] for t, p in sent if t == T_EFFECT]


def play_demo(hub, start=1000.0):
    now = start
    for delay, topic, payload in DEMO_SCRIPT:
        now += delay
        hub.handle(topic, payload, now)
    return now


def test_demo_match_produces_expected_lights():
    hub, sent = make_hub()
    play_demo(hub)
    assert effects(sent) == ["match", "elim", "elim", "knocked", "tilt", "elim", "victory", "lobby"]
    assert (T_NOTIFY, {"msg": 1}) in sent  # sensor board LED told a recap is waiting


def test_tilt_needs_both_signals():
    t = TiltDetector()
    assert not t.on_vibration(20, 1.0)            # slams alone: no tilt
    assert not t.on_game_event("knocked", 2.0)    # knocked alone: no tilt
    assert t.on_vibration(6, 3.0)                 # knocked + slams: tilt


def test_tilt_window_and_cooldown():
    t = TiltDetector(event_window=10, vib_window=5, vib_threshold=6, cooldown=30)
    t.on_game_event("knocked", 0.0)
    assert not t.on_vibration(6, 11.0)            # too long after the knock
    t.on_game_event("eliminated", 20.0)
    assert t.on_vibration(6, 21.0)
    t.on_game_event("knocked", 25.0)
    assert not t.on_vibration(10, 26.0)           # still in cooldown


def test_match_state_summary():
    m = MatchState()
    assert m.on_event("match_start", 0) is None
    m.on_event("elim", 10)
    m.on_event("knocked", 20)
    summary = m.on_event("eliminated", 30)
    assert summary["result"] == "loss" and summary["elims"] == 1 and summary["knocks"] == 1
    assert summary["duration_s"] == 30 and not m.in_match


def test_buttons_clip_and_study_mode():
    hub, sent = make_hub()
    hub.handle(T_BUTTON, {"id": "clip"}, 1.0)
    assert hub.db.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1
    hub.handle(T_BUTTON, {"id": "mode"}, 2.0)
    hub.handle(T_BUTTON, {"id": "mode"}, 3.0)
    hub.handle(T_BUTTON, {"id": "interact"}, 4.0)
    assert effects(sent) == ["clip", "study", "lobby", "ping"]


def test_companion_reacts_during_match():
    hub, _ = make_hub()
    moods = []
    hub.handle("desk/game/event", {"type": "match_start"}, 0.0)
    moods.append(hub.companion.mood)
    hub.handle("desk/game/event", {"type": "elim"}, 5.0)
    moods.append(hub.companion.mood)
    hub.handle("desk/game/event", {"type": "knocked"}, 60.0)
    moods.append(hub.companion.mood)
    assert moods == ["focused", "hype", "shocked"]


def test_late_night_is_time_based():
    from datetime import datetime
    from companion import Companion
    c = Companion()
    assert not c.check_late_night(datetime(2026, 11, 24, 22, 0))
    assert c.check_late_night(datetime(2026, 11, 25, 1, 30)) and c.mood == "sleepy"


def test_companion_recap_falls_back_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    hub, _ = make_hub()
    play_demo(hub)
    for _ in range(50):  # recap runs in a background thread
        if hub.companion.snapshot()["last_recap"]:
            break
        time.sleep(0.05)
    snap = hub.companion.snapshot()
    assert "3 elims" in snap["last_recap"]["text"]
    assert snap["session"] == {"games": 1, "wins": 1, "elims": 3, "tilts": 1}


def test_web_api():
    pytest.importorskip("flask")
    from web import create_app
    hub, _ = make_hub()
    play_demo(hub)
    client = create_app(hub).test_client()
    state = client.get("/api/state").get_json()
    assert state["matches"][0]["result"] == "win"
    feed = client.get("/api/events?since=0").get_json()
    assert any(e["kind"] == "effect" and e["data"]["effect"] == "victory" for e in feed["events"])
    assert client.get(f"/api/events?since={feed['last_id']}").get_json()["events"] == []
    assert client.get("/").status_code == 200


def test_eink_render():
    pytest.importorskip("PIL")
    from eink import render, MOODS
    from companion import MOODS as REACTION_MOODS
    assert {m for ms in REACTION_MOODS.values() for m in ms} <= set(MOODS)  # every mood has a face
    for mood in MOODS:
        img = render({"mood": mood, "message": "test", "session": {}, "sensors": {"temp_c": 22.5}}, (800, 480))
        assert img.size == (800, 480)


# ---------- detector ----------
from detector import ConfirmFilter  # noqa: E402


def test_confirm_filter_counts_each_appearance_once():
    c = ConfirmFilter(frames=3, cooldown=3.0)
    out = [c.update({"elim"}, t) for t in (0, 0.1, 0.2, 0.3, 3.5)]
    assert out == [[], [], ["elim"], [], []]       # held banner = one event
    c.update(set(), 3.6)
    out = [c.update({"elim"}, t) for t in (3.7, 3.8, 3.9)]
    assert out[-1] == ["elim"]                     # new appearance after cooldown


cv2 = pytest.importorskip("cv2")


def _scene(seed):
    r = np.random.default_rng(seed)
    img = np.zeros((1080, 1920, 3), np.uint8)
    for c in range(3):
        img[..., c] = np.linspace(r.integers(0, 120), r.integers(80, 255), 1920)[None, :].astype(np.uint8)
    for _ in range(40):
        cv2.circle(img, (int(r.integers(0, 1920)), int(r.integers(0, 1080))), int(r.integers(20, 150)),
                   tuple(int(x) for x in r.integers(0, 255, 3)), -1)
    img = cv2.GaussianBlur(img, (7, 7), 0)
    return cv2.add(img, r.integers(0, 15, (1080, 1920, 3), dtype=np.uint8))


def _put(img, text, y, color, x=560):
    """HUD-style text: colored fill with a thick dark outline."""
    mask = np.zeros(img.shape[:2], np.uint8)
    cv2.putText(mask, text, (x, y), cv2.FONT_HERSHEY_DUPLEX, 3.2, 255, 7, cv2.LINE_AA)
    img[cv2.dilate(mask, np.ones((11, 11), np.uint8)) > 0] = (0, 0, 0)
    a = (mask.astype(np.float32) / 255)[..., None]
    img[:] = (img * (1 - a) + np.array(color, np.float32) * a).astype(np.uint8)
    return img


def _crop(img, text, y, x=560):
    (w, h), b = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, 3.2, 7)
    return img[y - h - 8:y + b + 8, x - 8:x + w + 8]


def test_template_matching_on_synthetic_frames(tmp_path):
    import detector as d
    gold, white = (40, 215, 255), (255, 255, 255)
    for event in ("victory", "knocked"):
        (tmp_path / event).mkdir()
    cv2.imwrite(str(tmp_path / "victory" / "a.png"),
                _crop(_put(_scene(1), "VICTORY ROYALE", 300, gold), "VICTORY ROYALE", 300))
    cv2.imwrite(str(tmp_path / "knocked" / "a.png"),
                _crop(_put(_scene(2), "KNOCKED", 500, white), "KNOCKED", 500))
    templates = d.load_templates(str(tmp_path))

    for seed in range(20, 25):  # different backgrounds each time
        win = d.detect(_put(_scene(seed), "VICTORY ROYALE", 300, gold), templates)
        knock = d.detect(_put(_scene(seed + 100), "KNOCKED", 500, white), templates)
        other = d.detect(_put(_scene(seed + 200), "STORM IS SHRINKING", 300, white), templates)
        assert win["victory"] >= d.THRESHOLDS["victory"] > win["knocked"]
        assert knock["knocked"] >= d.THRESHOLDS["knocked"] > knock["victory"]
        assert max(other.values()) < 0.8

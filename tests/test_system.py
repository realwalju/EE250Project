"""Automated tests for the parts that don't need hardware.

Run from the repo root:  python -m pytest tests -q

Covers: tilt detection, match state, the hub's light effects for a full
fake match, the web API, the detector's multi-frame filter, and template
matching on synthetic game frames.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import json
import os
import sys
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "pc"))

from hub import Hub, MatchState, TiltDetector, T_BUTTON, T_EFFECT, T_GAME, T_NOTIFY, T_SENSORS  # noqa: E402
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


def _game(hub, etype, t):
    hub.handle(T_GAME, {"type": etype}, t)


def _saved_matches(hub):
    return [json.loads(r[0]) for r in hub.db.execute("SELECT summary FROM matches ORDER BY ts")]


def test_teammate_death_after_mine_is_not_a_new_match():
    hub, sent = make_hub()
    _game(hub, "match_start", 0)
    _game(hub, "elim", 60)
    _game(hub, "eliminated", 200)      # I die, now spectating
    n = len(sent)
    _game(hub, "eliminated", 320)      # teammate's death screen while spectating
    _game(hub, "eliminated", 400)      # squad's "You placed #N" screen
    assert len(sent) == n              # no light, no recap
    saved = _saved_matches(hub)
    assert len(saved) == 1 and saved[0]["elims"] == 1 and saved[0]["result"] == "loss"
    assert hub.match.match_id == 1


def test_reboot_continues_the_same_match():
    hub, sent = make_hub()
    _game(hub, "match_start", 0)
    _game(hub, "elim", 60)
    _game(hub, "eliminated", 200)
    _game(hub, "elim", 400)            # rebooted and got a kill
    assert hub.match.in_match and hub.match.match_id == 1
    assert effects(sent)[-2:] == ["match", "elim"]  # light back to in-match
    _game(hub, "eliminated", 600)
    saved = _saved_matches(hub)
    assert len(saved) == 1             # same match, summary updated
    assert saved[0]["elims"] == 2 and saved[0]["reboots"] == 1 and saved[0]["duration_s"] == 600


def test_team_win_after_my_death_counts_as_a_win():
    hub, _ = make_hub()
    _game(hub, "match_start", 0)
    _game(hub, "eliminated", 300)
    _game(hub, "victory", 900)
    assert [m["result"] for m in _saved_matches(hub)] == ["win"]
    assert hub.match.state == "lobby"


def test_next_bus_after_dying_starts_a_new_match():
    hub, _ = make_hub()
    _game(hub, "match_start", 0)
    _game(hub, "eliminated", 300)
    _game(hub, "match_start", 500)
    assert hub.match.in_match and hub.match.match_id == 2


def test_match_ids_continue_after_a_hub_restart(tmp_path):
    db = str(tmp_path / "desk.db")
    hub = Hub(lambda *a: None, db)
    _game(hub, "match_start", 0)
    _game(hub, "eliminated", 100)
    hub = Hub(lambda *a: None, db)     # Pi restarted
    _game(hub, "match_start", 500)
    _game(hub, "eliminated", 600)
    assert sorted(m["match_id"] for m in _saved_matches(hub)) == [1, 2]


def test_death_screen_with_no_match_running_is_ignored():
    hub, sent = make_hub()
    _game(hub, "eliminated", 10)
    _game(hub, "victory", 20)
    assert not sent and not _saved_matches(hub)


def test_knocked_then_eliminated_counts_one_knock():
    hub, sent = make_hub()
    _game(hub, "match_start", 0)
    _game(hub, "knocked", 100)
    _game(hub, "knocked", 106)         # red bar seen again while still down
    _game(hub, "eliminated", 130)
    _game(hub, "knocked", 133)         # red bar still detected on the death screen
    _game(hub, "eliminated", 140)      # death screen seen again
    _game(hub, "knocked", 160)
    saved = _saved_matches(hub)
    assert len(saved) == 1 and saved[0]["knocks"] == 1 and saved[0]["reboots"] == 0
    assert hub.match.state == "dead"


def test_spectated_teammate_knock_plays_the_light_but_isnt_counted():
    hub, sent = make_hub()
    _game(hub, "match_start", 0)
    _game(hub, "eliminated", 100)      # I'm out, spectating
    n = len(sent)
    _game(hub, "knocked", 200)         # teammate goes down
    assert effects(sent[n:]) == ["knocked"]
    assert hub.match.state == "dead" and hub.match.stats["knocks"] == 0
    assert _saved_matches(hub)[0]["knocks"] == 0


def test_knocked_again_after_a_revive_counts():
    hub, _ = make_hub()
    _game(hub, "match_start", 0)
    _game(hub, "knocked", 100)
    _game(hub, "knocked", 160)         # revived, knocked again later
    assert hub.match.stats["knocks"] == 2


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
    assert moods == ["focused", "excited", "surprised"]


def test_late_night_is_time_based():
    from datetime import datetime
    from companion import Companion
    c = Companion()
    assert not c.check_late_night(datetime(2026, 11, 24, 22, 0))
    assert c.check_late_night(datetime(2026, 11, 25, 1, 30)) and c.mood == "sleepy"
    c.react("interact")
    assert not c.check_late_night(datetime(2026, 11, 25, 1, 45))
    assert c.check_late_night(datetime(2026, 11, 25, 2, 0))


def test_multi_elim_window_and_new_match_reset(monkeypatch):
    from companion import Companion, REACTIONS

    requests = []
    c = Companion(on_update=requests.append)
    monkeypatch.setattr("companion.random.choice", lambda choices: choices[0])
    c.on_game_event("match_start", 0)
    c.on_game_event("elim", 5)
    assert c.message in REACTIONS["elim"] and c.mood == "excited"
    c.on_game_event("elim", 10)
    assert c.message in REACTIONS["multi"] and c.mood == "excited"
    c.on_game_event("elim", 25)
    assert c.message in REACTIONS["elim"]
    c.on_game_event("match_start", 26)
    c.on_game_event("elim", 27)
    assert c.message in REACTIONS["elim"]
    assert requests and not any(requests)  # every in-match reaction is soft


def test_game_faces_are_rate_limited_but_interaction_and_results_are_forced(monkeypatch):
    requests = []

    class Display:
        def request(self, state, force=False):
            requests.append((state, force))

    hub = Hub(lambda *_: None, ":memory:", display=Display())
    # This test checks the immediate reaction, independent of recap completion.
    monkeypatch.setattr(hub.companion, "_recap", lambda *_: None)
    for index, (event, mood) in enumerate(
            (("match_start", "focused"), ("elim", "excited"), ("knocked", "surprised"))):
        hub.handle(T_GAME, {"type": event}, index)
        state, force = requests[-1]
        assert state["mood"] == mood
        assert state["in_match"] and not force
    assert hub.match.stats["elims"] == 1 and hub.match.stats["knocks"] == 1

    hub.handle(T_BUTTON, {"id": "clip"}, 3)
    assert requests[-1][0]["mood"] == "excited" and not requests[-1][1]
    hub.handle(T_BUTTON, {"id": "interact"}, 4)
    assert requests[-1][0]["mood"] == "playful" and requests[-1][1]
    hub.handle(T_GAME, {"type": "victory"}, 5)
    assert requests[-1][0]["mood"] == "happy" and requests[-1][1]
    assert not requests[-1][0]["in_match"]


@pytest.mark.parametrize("choice_index,tilt_mood,loss_mood",
                         [(0, "worried", "smug"), (-1, "annoyed", "sad")])
def test_tilt_and_loss_offer_both_expressions(monkeypatch, choice_index, tilt_mood, loss_mood):
    from companion import Companion

    monkeypatch.setattr("companion.random.choice", lambda choices: choices[choice_index])
    companion = Companion()
    companion.react("tilt")
    assert companion.snapshot()["mood"] == tilt_mood
    assert companion.snapshot()["session"]["tilts"] == 1
    companion.react("loss")
    assert companion.snapshot()["mood"] == loss_mood


def _defer_recaps(monkeypatch):
    """Hold background work so tests can complete network responses out of order."""
    jobs = []

    class DeferredThread:
        def __init__(self, target, args, daemon):
            jobs.append(lambda: target(*args))

        def start(self):
            pass

    monkeypatch.setattr("companion.threading.Thread", DeferredThread)
    return jobs


def _summary(match_id):
    return {"match_id": match_id, "result": "win", "elims": 2,
            "knocks": 0, "duration_s": 120, "timeline": []}


def test_delayed_recap_preserves_new_game_face(monkeypatch):
    from companion import Companion

    jobs = _defer_recaps(monkeypatch)
    companion = Companion()
    monkeypatch.setattr(companion, "_llm_recap", lambda _: "Nice finish.")
    companion.on_match_end(_summary(1))
    companion.react("match_start")
    current = companion.snapshot()
    jobs[0]()
    updated = companion.snapshot()
    assert (updated["mood"], updated["message"]) == (current["mood"], current["message"])
    assert updated["last_recap"]["text"] == "Nice finish."


def test_older_match_recap_cannot_replace_newer_result(monkeypatch):
    from companion import Companion

    jobs = _defer_recaps(monkeypatch)
    companion = Companion()
    monkeypatch.setattr(companion, "_llm_recap", lambda s: f"Recap {s['match_id']}.")
    companion.on_match_end(_summary(1))
    companion.on_match_end(_summary(2))
    jobs[1]()
    jobs[0]()
    updated = companion.snapshot()
    assert updated["last_recap"]["match_id"] == 2
    assert updated["mood"] == "happy" and updated["message"] == "Recap 2."


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
from detector import ConfirmFilter, KillCounter  # noqa: E402


def test_confirm_filter_counts_each_appearance_once():
    c = ConfirmFilter(frames=3, cooldown=3.0)
    out = [c.update({"elim"}, t) for t in (0, 0.1, 0.2, 0.3, 3.5)]
    assert out == [[], [], ["elim"], [], []]       # held banner = one event
    c.update(set(), 3.6)
    assert c.update({"elim"}, 3.7) == []           # one missed frame: still the same banner
    c.update(set(), 3.8)
    c.update(set(), 3.9)                           # gone for 2 checks: next one is new
    out = [c.update({"elim"}, t) for t in (4.0, 4.1, 4.2)]
    assert out[-1] == ["elim"]                     # new appearance after cooldown


def test_confirm_filter_trusts_a_strong_single_frame():
    c = ConfirmFilter(frames=2, cooldown=3.0)
    assert c.update({"elim"}, 0.0, strong={"elim"}) == ["elim"]   # one 0.93 frame is enough
    assert c.update({"elim"}, 1.2) == []                          # same banner, not twice
    c.update(set(), 2.4)
    c.update(set(), 3.0)
    assert c.update({"elim"}, 3.6) == []                          # weak single frame: wait
    assert c.update({"elim"}, 4.8) == ["elim"]                    # ...confirmed by a 2nd frame


def test_confirm_filter_survives_one_bad_frame():
    """The bus prompt flickers with the map under it: 2 of 3 frames is enough."""
    c = ConfirmFilter(frames=2)
    assert c.update({"match_start"}, 0.0) == []
    assert c.update(set(), 0.2) == []
    assert c.update({"match_start"}, 0.4) == ["match_start"]


def test_confirm_filter_ignores_skipped_checks():
    c = ConfirmFilter(frames=2)
    c.update({"victory"}, 0.0)
    c.update(set(), 0.1, checked=set())            # victory not looked for this frame
    assert c.update({"victory"}, 0.2) == ["victory"]


def test_match_start_fires_once_per_bus_even_if_the_prompt_drops_out():
    import detector as d
    c = ConfirmFilter(frames=d.CONFIRM_FRAMES, cooldown=d.COOLDOWNS)
    sent = []
    # prompt up, hidden for 2 s mid-bus, back up for 20 s
    for i in range(300):
        t = i * 0.1
        seen = {"match_start"} if not 5 <= t < 7 else set()
        sent += c.update(seen, t)
    assert sent == ["match_start"]


def test_knocked_bar_dropping_out_counts_once():
    import detector as d
    c = ConfirmFilter(frames=d.CONFIRM_FRAMES, cooldown=d.COOLDOWNS, end_after=d.END_AFTER)
    sent = []
    # down for 40 s at 5 fps; the red bar drops out for 1 s every 6 s
    for i in range(200):
        t = i * 0.2
        sent += c.update(set() if t % 6 < 1 else {"knocked"}, t)
    assert sent == ["knocked"]
    # gone for good, then knocked again later: counts
    for i in range(200, 400):
        t = i * 0.2
        sent += c.update({"knocked"} if t >= 70 else set(), t)
    assert sent == ["knocked", "knocked"]


def _feed(counter, counts, dt=0.15, strong=False):
    return sum(counter.update(n, i * dt, strong=strong) for i, n in enumerate(counts))


def test_kill_counter_counts_back_to_back_kills():
    # ELIMINATION! banner, then it turns into DOUBLE ELIM! while still up
    assert _feed(KillCounter(), [1, 1, 1, 2, 2, 2, 0, 0, 0, 0]) == 2


def test_kill_counter_counts_two_popups_appearing_at_once():
    assert _feed(KillCounter(), [0, 2, 2, 2]) == 2


def test_kill_counter_ignores_a_flickering_banner():
    assert _feed(KillCounter(), [1, 1, 0, 1, 1, 1]) == 1   # one blurry frame


def test_kill_counter_counts_a_new_kill_after_the_last_one_faded():
    assert _feed(KillCounter(), [1, 1, 1] + [0] * 6 + [1, 1]) == 2


def test_kill_counter_needs_two_frames_unless_strong():
    assert _feed(KillCounter(), [0, 1, 0, 0, 0]) == 0          # one weak frame
    assert _feed(KillCounter(), [0, 1, 0, 0, 0], strong=True) == 1


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
    for event in ("victory", "eliminated"):
        (tmp_path / event).mkdir()
    cv2.imwrite(str(tmp_path / "victory" / "a.png"),
                _crop(_put(_scene(1), "VICTORY ROYALE", 300, gold), "VICTORY ROYALE", 300))
    cv2.imwrite(str(tmp_path / "eliminated" / "a.png"),
                _crop(_put(_scene(2), "KNOCKED", 500, white), "KNOCKED", 500))
    templates = d.load_templates(str(tmp_path))

    for seed in range(20, 25):  # different backgrounds each time
        win = d.detect(_put(_scene(seed), "VICTORY ROYALE", 300, gold), templates)
        knock = d.detect(_put(_scene(seed + 100), "KNOCKED", 500, white), templates)
        other = d.detect(_put(_scene(seed + 200), "STORM IS SHRINKING", 300, white), templates)
        assert win["victory"] >= d.THRESHOLDS["victory"] > win["eliminated"]
        assert knock["eliminated"] >= d.THRESHOLDS["eliminated"] > knock["victory"]
        assert max(other.values()) < 0.8


def test_detection_is_resolution_independent(tmp_path):
    """A template cropped from a 1920x1080 screenshot still matches the same HUD
    text on a 2880x1800 screen (and vice versa), because frames and templates
    are normalised by height."""
    import detector as d
    white = (255, 255, 255)
    small = _put(_scene(7), "ELIMINATED", 600, white)                 # 1920x1080 screen
    shot = tmp_path / "small.png"
    cv2.imwrite(str(shot), small)
    (w, h), b = cv2.getTextSize("ELIMINATED", cv2.FONT_HERSHEY_DUPLEX, 3.2, 7)
    d.save_template(str(shot), (552, 600 - h - 8, 568 + w, 600 + b + 8), "elim", "t", folder=str(tmp_path))
    templates = d.load_templates(str(tmp_path))

    big = cv2.resize(_put(_scene(8), "ELIMINATED", 600, white), (3200, 1800))  # same HUD, bigger screen
    assert d.detect(big, templates)["elim"] >= d.THRESHOLDS["elim"]
    assert d.detect(cv2.resize(_scene(9), (3200, 1800)), templates)["elim"] < 0.6


def test_repeated_match_start_on_the_bus_is_ignored():
    hub, _ = make_hub()
    hub.handle("desk/game/event", {"type": "match_start"}, 0.0)
    first = hub.match.match_id
    hub.handle("desk/game/event", {"type": "elim"}, 5.0)
    hub.handle("desk/game/event", {"type": "match_start"}, 12.0)  # prompt seen again
    assert hub.match.match_id == first and hub.match.stats["elims"] == 1


def _popup(img, y, x=1400):
    """Small "ENEMY ELIMINATED"-style popup line on a dark plate (right side)."""
    cv2.rectangle(img, (x - 10, y - 40), (x + 470, y + 12), (40, 30, 30), -1)
    cv2.putText(img, "ENEMY ELIMINATED", (x, y), cv2.FONT_HERSHEY_DUPLEX, 1.4, (255, 255, 255), 3, cv2.LINE_AA)
    return img


def test_stacked_kill_popups_are_counted(tmp_path):
    """Two "ENEMY ELIMINATED" popups stacked on the right = 2 kills on screen."""
    import detector as d
    (tmp_path / "elim").mkdir()
    cv2.imwrite(str(tmp_path / "elim" / "enemy_eliminated.png"), _popup(_scene(3), 500)[455:517, 1385:1885])
    templates = d.load_templates(str(tmp_path))

    single = d.detect(_popup(_scene(4), 520), templates)
    double = d.detect(_popup(_popup(_scene(5), 460), 540), templates)
    none = d.detect(_scene(6), templates)
    assert single.kills == 1
    assert double.kills == 2
    assert none.kills == 0


def test_white_text_prompt_matches_over_any_background(tmp_path):
    """The bus prompt is white text straight on the map; it must score the same
    whatever is underneath."""
    import detector as d
    white = (255, 255, 255)
    (tmp_path / "match_start").mkdir()
    shot = _put(_scene(10), "TO JUMP", 560, white, x=1000)
    cv2.imwrite(str(tmp_path / "match_start" / "to_jump.png"), _crop(shot, "TO JUMP", 560, x=1000))
    templates = d.load_templates(str(tmp_path))
    for seed in range(30, 35):
        assert d.detect(_put(_scene(seed), "TO JUMP", 560, white, x=1000), templates)["match_start"] \
            >= d.THRESHOLDS["match_start"]
        assert d.detect(_put(_scene(seed), "TO HURL", 560, white, x=1000), templates)["match_start"] \
            < d.THRESHOLDS["match_start"]

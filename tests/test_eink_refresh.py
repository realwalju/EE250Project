"""Deterministic refresh scheduling checks; no display, sleeps, or game CV."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))

import eink


def _display(monkeypatch):
    clock, workers, timers, shown = [100.0], [], [], []

    def thread(target, daemon):
        return SimpleNamespace(start=lambda: workers.append(target))

    def timer(delay, callback):
        return SimpleNamespace(start=lambda: timers.append((delay, callback)))

    backend = SimpleNamespace(size=(800, 480), name="fake", show=shown.append)
    monkeypatch.setattr(eink, "threading",
                        SimpleNamespace(Thread=thread, Timer=timer, Lock=threading.Lock))
    monkeypatch.setattr(eink, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(eink, "_Waveshare", lambda *_: backend)
    monkeypatch.setattr(eink, "render", lambda state, size: state["mood"])
    return SimpleNamespace(display=eink.EinkDisplay(min_interval=20, model="epd7in3e"), backend=backend,
                           clock=clock, workers=workers, timers=timers, shown=shown)


def test_soft_update_during_refresh_gets_its_own_scheduled_refresh(monkeypatch):
    h = _display(monkeypatch)

    def show(mood):
        h.shown.append(mood)
        if len(h.shown) == 1:
            h.display.request({"mood": "excited"})

    h.backend.show = show
    h.display.request({"mood": "focused"})
    h.workers.pop(0)()
    assert h.shown == ["focused"]
    assert not h.display.busy and len(h.timers) == 1
    delay, wake = h.timers.pop(0)
    assert delay == 20

    h.clock[0] += delay
    wake()
    h.workers.pop(0)()
    assert h.shown == ["focused", "excited"]
    assert h.display.pending is None and not h.display.busy


def test_old_timer_rechecks_interval_after_forced_refresh(monkeypatch):
    h = _display(monkeypatch)
    h.display.last_refresh = 95
    h.display.request({"mood": "excited"})
    delay, old_wake = h.timers.pop(0)
    assert delay == 15 and not h.workers

    h.clock[0] = 105
    h.display.request({"mood": "happy"}, force=True)
    h.workers.pop(0)()
    h.clock[0] = 106
    h.display.request({"mood": "surprised"})

    h.clock[0] = 115
    old_wake()
    assert h.shown == ["happy"] and not h.workers
    delay, wake = h.timers.pop(0)
    assert delay == 10

    h.clock[0] = 125
    wake()
    h.workers.pop(0)()
    assert h.shown == ["happy", "surprised"]
    assert h.display.pending is None and not h.display.busy

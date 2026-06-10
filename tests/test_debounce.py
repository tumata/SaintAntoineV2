"""Hold-to-trigger debounce on virtual time: glitches die, real presses pass."""

import threading

from saintantoine.debounce import PressDetector

from conftest import FakeClock


class ScriptedPin:
    """Pin pressed during the given [start, end) windows of virtual time."""

    def __init__(self, clock: FakeClock, windows):
        self.clock = clock
        self.windows = windows

    def __call__(self) -> bool:
        return any(s <= self.clock.t < e for s, e in self.windows)


def make_detector(clock, pin, presses, **kwargs):
    params = dict(
        hold_window_ms=50,
        sample_interval_ms=5,
        min_press_interval_ms=300,
        rapidfire_count=3,
        rapidfire_window_s=1.0,
        rapidfire_cooldown_s=2.0,
    )
    params.update(kwargs)
    return PressDetector(
        is_pressed=pin,
        on_press=lambda: presses.append(clock.t),
        clock=clock.clock,
        sleep=clock.sleep,
        stop_event=threading.Event(),
        **params,
    )


def run_until(detector, clock, t_end):
    while clock.t < t_end:
        detector.poll_once()


def test_glitch_rejected():
    clock = FakeClock()
    presses = []
    # 20 ms spike — shorter than the 50 ms hold window
    det = make_detector(clock, ScriptedPin(clock, [(0.0, 0.02)]), presses)
    run_until(det, clock, 1.0)
    assert presses == []


def test_genuine_press_accepted_once():
    clock = FakeClock()
    presses = []
    det = make_detector(clock, ScriptedPin(clock, [(0.0, 0.5)]), presses)
    run_until(det, clock, 1.0)
    assert len(presses) == 1
    assert 0.04 <= presses[0] <= 0.08  # accepted right after the hold window


def test_no_autorepeat_while_held():
    clock = FakeClock()
    presses = []
    det = make_detector(clock, ScriptedPin(clock, [(0.0, 5.0)]), presses)
    run_until(det, clock, 5.0)
    assert len(presses) == 1


def test_min_interval_enforced():
    clock = FakeClock()
    presses = []
    # Second press starts 150 ms after the first ends — under the 300 ms min interval
    det = make_detector(clock, ScriptedPin(clock, [(0.0, 0.1), (0.25, 0.35)]), presses)
    run_until(det, clock, 1.0)
    assert len(presses) == 1


def test_spaced_presses_all_accepted():
    clock = FakeClock()
    presses = []
    windows = [(0.0, 0.1), (1.0, 1.1), (2.0, 2.1)]
    det = make_detector(clock, ScriptedPin(clock, windows), presses)
    run_until(det, clock, 3.0)
    assert len(presses) == 3


def test_rapidfire_lockout():
    clock = FakeClock()
    presses = []
    # 5 valid presses in quick succession; permissive min-interval so only the
    # rapid-fire guard can stop them
    windows = [(i * 0.2, i * 0.2 + 0.1) for i in range(5)]
    det = make_detector(
        clock,
        ScriptedPin(clock, windows),
        presses,
        min_press_interval_ms=10,
        rapidfire_count=3,
        rapidfire_window_s=1.0,
        rapidfire_cooldown_s=2.0,
    )
    run_until(det, clock, 1.2)
    assert len(presses) == 3  # 4th hit the lockout


def test_glitch_then_real_press():
    clock = FakeClock()
    presses = []
    det = make_detector(clock, ScriptedPin(clock, [(0.0, 0.01), (0.5, 0.7)]), presses)
    run_until(det, clock, 1.0)
    assert len(presses) == 1
    assert presses[0] >= 0.5

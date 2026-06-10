"""Audio watchdog: re-init recovery, safe-IDLE policy, escalation to exit."""

from saintantoine.controller import IDLE, PLAYING


def test_play_raises_triggers_reinit_and_safe_idle(harness):
    harness.audio.fail_on_play = True

    harness.press()

    assert harness.audio.reinit_count >= 1
    assert harness.controller.state == IDLE
    assert harness.gpio.relay_states() == [False, False, False]
    assert harness.controller.audio_healthy is True  # reinit fixed it
    assert harness.shutdown_calls == []


def test_playback_never_starts_grace_fault(harness):
    harness.audio.silent = True  # play "succeeds" but never reports busy

    harness.press()
    assert harness.controller.state == PLAYING
    harness.run_until(harness.cfg.play_start_grace_s + 1.0)

    assert harness.audio.reinit_count >= 1
    assert harness.controller.state == IDLE
    assert harness.gpio.relay_states() == [False, False, False]


def test_idle_health_probe_detects_fault(harness):
    harness.audio.healthy = False

    harness.run_until(harness.cfg.health_probe_interval_s + 1.0)

    assert harness.audio.reinit_count >= 1
    assert harness.controller.audio_healthy is True


def test_escalation_after_repeated_reinit_failures(harness):
    harness.audio.healthy = False
    harness.audio.fail_reinit = True  # every reinit raises

    harness.run_until(harness.cfg.health_probe_interval_s + 1.0)

    assert harness.audio.reinit_count == harness.cfg.max_reinit_attempts
    assert harness.controller.audio_healthy is False
    assert harness.shutdown_calls == [1]  # non-zero exit requested for systemd
    assert harness.gpio.relay_states() == [False, False, False]


def test_no_escalation_when_disabled(make_harness):
    h = make_harness()
    h.cfg.escalate_exit = False
    h.audio.healthy = False
    h.audio.fail_reinit = True

    h.run_until(h.cfg.health_probe_interval_s + 1.0)

    assert h.controller.audio_healthy is False
    assert h.shutdown_calls == []


def test_resume_policy_replays_track(make_harness):
    h = make_harness()
    h.cfg.on_audio_fault = "resume"
    h.press()
    track = h.audio.current_path
    h.run_until(1.0)
    assert h.controller.state == PLAYING

    h.controller._handle_audio_fault("test wedge")

    assert h.controller.state == PLAYING
    assert h.gpio.relay_states() == [True, True, True]
    assert h.audio.current_path == track

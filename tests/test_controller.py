"""State machine: press/switch/natural-end/shutdown, relay invariant, file checks."""

import os

from saintantoine.controller import IDLE, PLAYING


def test_idle_press_starts_playing(harness):
    assert harness.controller.state == IDLE
    assert harness.gpio.relay_states() == [False, False, False]

    harness.press()

    assert harness.controller.state == PLAYING
    assert harness.gpio.relay_states() == [True, True, True]
    assert harness.audio.current_path in harness.tracks


def test_press_while_playing_switches_track_relays_stay_on(harness):
    harness.press()
    first = harness.audio.current_path
    harness.clock.advance(1.0)

    harness.press()

    assert harness.controller.state == PLAYING
    assert harness.gpio.relay_states() == [True, True, True]
    assert harness.audio.current_path != first


def test_natural_end_turns_relays_off(harness):
    harness.press()
    harness.run_until(1.0)  # ticks while busy -> went_busy latched
    assert harness.controller.state == PLAYING

    harness.run_until(11.0)  # mock track duration is 10 s

    assert harness.controller.state == IDLE
    assert harness.gpio.relay_states() == [False, False, False]
    assert harness.controller.status()["current_track"] is None


def test_press_after_natural_end_starts_again(harness):
    harness.press()
    harness.run_until(11.0)
    assert harness.controller.state == IDLE

    harness.press()
    assert harness.controller.state == PLAYING
    assert harness.gpio.relay_states() == [True, True, True]


def test_shutdown_stops_everything(harness):
    harness.press()
    harness.controller.shutdown()

    assert harness.controller.state == IDLE
    assert harness.gpio.relay_states() == [False, False, False]
    assert harness.audio.current_path is None


def test_missing_file_skipped(make_harness):
    h = make_harness(n_tracks=2)
    victim = h.tracks[0]
    os.remove(victim)

    # Two presses guarantee the missing file is encountered (a 2-track bag
    # cycle contains both), gets skipped, and playback only ever uses the
    # existing file.
    h.press()
    assert h.controller.state == PLAYING
    assert h.audio.current_path == h.tracks[1]

    h.clock.advance(1.0)
    h.press()
    assert h.controller.state == PLAYING
    assert h.audio.current_path == h.tracks[1]  # only playable track
    assert victim not in h.bag.tracks  # dropped from the pool


def test_all_files_missing_stays_idle(make_harness):
    h = make_harness(n_tracks=2)
    for t in h.tracks:
        os.remove(t)

    h.press()

    assert h.controller.state == IDLE
    assert h.gpio.relay_states() == [False, False, False]


def test_empty_library_press_is_safe(make_harness):
    h = make_harness(n_tracks=0)
    h.press()
    assert h.controller.state == IDLE
    assert h.gpio.relay_states() == [False, False, False]


def test_switch_never_replays_current(make_harness):
    h = make_harness(n_tracks=3)
    h.press()
    for _ in range(20):
        current = h.audio.current_path
        h.clock.advance(0.5)
        h.press()
        assert h.audio.current_path != current


def test_status_shape(harness):
    s = harness.controller.status()
    assert s["state"] == IDLE
    assert s["relays"] == [False, False, False]
    assert s["mode"] == "mock"
    assert s["audio_healthy"] is True
    assert s["tracks_total"] == 3

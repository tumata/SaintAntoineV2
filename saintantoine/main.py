"""Entrypoint: wiring, lifecycle, signals, startup delay (SPECS §6, §15).

One process. Clean shutdown (SIGTERM/SIGINT or POST /restart) stops playback,
turns relays OFF, releases audio, and exits — systemd Restart=always respawns.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from typing import List, Optional

from . import __version__
from .audio import AudioError, create_audio_backend
from .config import load_config, resolve_log_file, resolve_music_folder
from .controller import Controller
from .debounce import PressDetector
from .gpio import MockGpio, RealGpio
from .logging_setup import setup_logging
from .platform_detect import resolve_mode
from .selection import ShuffleBag, scan_tracks
from .webhook import Webhook

log = logging.getLogger(__name__)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="saintantoine",
                                     description="Button → 3 relays + music controller")
    parser.add_argument("--config", help="path to config.yaml")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--real", dest="mode", action="store_const", const="real",
                            help="force real mode (Pi GPIO + audio)")
    mode_group.add_argument("--mock", dest="mode", action="store_const", const="mock",
                            help="force mock mode (no Pi needed)")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    mode = resolve_mode(args.mode, cfg.mode)

    ring = setup_logging(cfg, resolve_log_file(cfg, mode))
    log.info("Saint Antoine v%s starting in %s mode.", __version__, mode.upper())

    if mode == "real" and cfg.startup_delay_s > 0:
        log.info("Startup delay: %.1f s (audio/network settle).", cfg.startup_delay_s)
        time.sleep(cfg.startup_delay_s)

    # --- backends ---------------------------------------------------------
    if mode == "real":
        gpio = RealGpio(cfg)
    else:
        gpio = MockGpio(relay_count=len(cfg.relay_pins))
        log.info("Mock GPIO: use the web dashboard to fake button presses.")

    audio = create_audio_backend(mode, cfg)
    try:
        audio.init()
    except AudioError as e:
        # The watchdog's health probe will keep retrying / escalate
        log.critical("Audio init failed at startup: %s", e)

    # --- music library ----------------------------------------------------
    folder = resolve_music_folder(cfg, mode)
    tracks = scan_tracks(folder, cfg.audio_extensions)
    if tracks:
        log.info("Found %d track(s) in %s.", len(tracks), folder)
    else:
        log.error("No playable tracks in %s — dashboard stays up, presses will be ignored.",
                  folder)
    bag = ShuffleBag(tracks)

    # --- lifecycle --------------------------------------------------------
    shutdown_event = threading.Event()
    exit_code = {"code": 0}

    def request_shutdown(code: int = 0) -> None:
        if code:
            exit_code["code"] = code
        shutdown_event.set()

    webhook = Webhook(cfg.webhook_url, cfg.webhook_timeout_s)
    if webhook.enabled:
        log.info("Webhook enabled: %s", cfg.webhook_url)

    controller = Controller(gpio, audio, bag, webhook, cfg, mode,
                            request_shutdown=request_shutdown)

    stop_threads = threading.Event()
    threads = []

    detector = PressDetector(
        is_pressed=gpio.is_button_pressed,
        on_press=controller.on_press,
        hold_window_ms=cfg.hold_window_ms,
        sample_interval_ms=cfg.sample_interval_ms,
        min_press_interval_ms=cfg.min_press_interval_ms,
        rapidfire_count=cfg.rapidfire_count,
        rapidfire_window_s=cfg.rapidfire_window_s,
        rapidfire_cooldown_s=cfg.rapidfire_cooldown_s,
        stop_event=stop_threads,
    )
    threads.append(threading.Thread(target=detector.run, name="button", daemon=True))
    threads.append(threading.Thread(target=controller.run_supervisor, name="supervisor",
                                    args=(stop_threads,), daemon=True))

    if cfg.web_enabled:
        from .web.server import create_app, run_web_server

        app = create_app(controller, ring, lambda: request_shutdown(0), cfg)
        threads.append(threading.Thread(target=run_web_server, name="web",
                                        args=(app, cfg), daemon=True))

    def handle_signal(signum, frame):
        log.info("Received signal %s — shutting down.", signal.Signals(signum).name)
        request_shutdown(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    for t in threads:
        t.start()
    log.info("Ready. Press the button to play/change music.")

    shutdown_event.wait()

    # --- clean shutdown: stop playback, relays OFF, release audio ----------
    stop_threads.set()
    for t in threads:
        if t.name != "web":  # flask has no clean stop; it's a daemon thread
            t.join(timeout=2.0)
    controller.shutdown()
    try:
        audio.quit()
    except Exception as e:
        log.error("Audio quit failed: %s", e)
    try:
        gpio.close()
    except Exception as e:
        log.error("GPIO close failed: %s", e)

    log.info("Exit (code %d).", exit_code["code"])
    return exit_code["code"]


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()

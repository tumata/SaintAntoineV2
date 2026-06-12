"""Entrypoint: CLI flags, mode selection, lifecycle, signals.

SPECS_BUTTONSCOPE §7, §8. On-demand diagnostic tool — no systemd unit; if the
initial pin claim fails, say why (probably the saintantoine service) and exit.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import List, Optional

from saintantoine.platform_detect import is_raspberry_pi

from . import __version__
from .sampler import PULLS, MockSignal, RealInput, Sampler
from .web import create_app, run_web_server

log = logging.getLogger(__name__)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="buttonscope",
        description="Live button-input voltage viewer (digital sampling, web graph)")
    parser.add_argument("--pin", type=int, default=22,
                        help="initial BCM pin to observe (changeable live, default 22)")
    parser.add_argument("--pull", choices=PULLS, default="up",
                        help="initial pull (changeable live, default up — matches "
                             "the button-to-GND wiring, SPECS §4)")
    parser.add_argument("--port", type=int, default=8081, help="web port (default 8081)")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--sample-hz", type=float, default=1000.0,
                        help="target polling rate, best effort (default 1000)")
    parser.add_argument("--window-s", type=float, default=10.0,
                        help="graph window in seconds (default 10)")
    parser.add_argument("--glitch-ms", type=float, default=20.0,
                        help="pulses shorter than this count as glitches (default 20)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--real", dest="mode", action="store_const", const="real",
                            help="force real mode (Pi GPIO)")
    mode_group.add_argument("--mock", dest="mode", action="store_const", const="mock",
                            help="force mock mode (synthetic signal)")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    mode = args.mode or ("real" if is_raspberry_pi() else "mock")
    log.info("ButtonScope v%s starting in %s mode.", __version__, mode.upper())

    factory = RealInput if mode == "real" else MockSignal
    try:
        sampler = Sampler(factory, args.pin, args.pull,
                          sample_hz=args.sample_hz, glitch_ms=args.glitch_ms,
                          retention_s=args.window_s + 5.0)
    except Exception as e:
        log.critical(
            "GPIO%d is busy or unavailable (%s) — is the saintantoine service "
            "running? Stop it first: sudo systemctl stop saintantoine",
            args.pin, e)
        return 1
    log.info("Observing GPIO%d (pull-%s) at %.0f Hz.", args.pin, args.pull, args.sample_hz)

    shutdown_event = threading.Event()
    stop_threads = threading.Event()

    def handle_signal(signum, frame):
        log.info("Received signal %s — shutting down.", signal.Signals(signum).name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    app = create_app(sampler, mode, window_s=args.window_s)
    threads = [
        threading.Thread(target=sampler.run, name="sampler",
                         args=(stop_threads,), daemon=True),
        threading.Thread(target=run_web_server, name="web",
                         args=(app, args.host, args.port), daemon=True),
    ]
    for t in threads:
        t.start()

    shutdown_event.wait()

    stop_threads.set()
    threads[0].join(timeout=2.0)  # flask has no clean stop; it's a daemon thread
    sampler.close()
    log.info("Exit.")
    return 0


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()

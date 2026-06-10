"""Logging: rotating file + stdout (journald) + in-memory ring buffer for the dashboard."""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Tuple

from .config import Config

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted log lines in memory, each with a sequence
    number so the dashboard can stream only what it hasn't seen yet."""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self._entries: deque = deque(maxlen=capacity)
        self._seq = 0
        self._lock_buf = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lock_buf:
            self._seq += 1
            self._entries.append((self._seq, line))

    def since(self, seq: int) -> List[Tuple[int, str]]:
        with self._lock_buf:
            return [e for e in self._entries if e[0] > seq]

    def latest_seq(self) -> int:
        with self._lock_buf:
            return self._seq


def setup_logging(cfg: Config, log_file: Path) -> RingBufferHandler:
    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    ring = RingBufferHandler(cfg.log_ring_buffer_size)
    ring.setFormatter(formatter)
    root.addHandler(ring)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backup_count
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        root.warning("Cannot open log file %s (%s); file logging disabled.", log_file, e)

    # Flask/werkzeug request logs are noisy; keep them out of INFO
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return ring

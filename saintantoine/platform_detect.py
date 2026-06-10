"""Raspberry Pi detection and run-mode resolution.

(Named platform_detect rather than platform to avoid shadowing the stdlib module.)
"""

from __future__ import annotations

from typing import Optional


def is_raspberry_pi() -> bool:
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as fh:
            if "raspberry pi" in fh.read().lower():
                return True
    except OSError:
        pass
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as fh:
            return "raspberry pi" in fh.read().lower()
    except OSError:
        return False


def resolve_mode(cli_mode: Optional[str], cfg_mode: str) -> str:
    """Returns "real" or "mock". CLI flag wins over config; "auto" detects."""
    mode = cli_mode or cfg_mode
    if mode == "auto":
        return "real" if is_raspberry_pi() else "mock"
    return mode

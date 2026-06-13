"""Configuration: typed defaults, YAML file, environment overrides.

Precedence (lowest to highest): dataclass defaults -> config.yaml -> SAINTANTOINE_* env vars.
The --mock/--real CLI flags override `mode` on top of everything (handled in main.py).
"""

from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_PREFIX = "SAINTANTOINE_"

DEFAULT_PI_MUSIC_FOLDER = "/home/pi/musique"
DEFAULT_PI_LOG_FILE = "/home/pi/saintantoine/log_script.txt"
DEFAULT_PI_ANALYTICS_DB = "/home/pi/saintantoine/analytics.db"


@dataclass
class Config:
    # Mode: auto-detect Pi, or force with "real"/"mock" (CLI --real/--mock wins)
    mode: str = "auto"

    # Music
    music_folder: str = DEFAULT_PI_MUSIC_FOLDER
    mock_music_folder: str = "MockMusics"
    audio_extensions: List[str] = field(default_factory=lambda: [".mp3", ".wav", ".ogg", ".flac"])

    # GPIO (BCM numbering)
    button_pin: int = 22
    # Pull-up: button wired GPIO ↔ GND, pressed = LOW (ButtonScope diagnosis
    # 2026-06-12 — pull-down fought the external pull-up resistor; SPECS §4)
    button_pull_up: bool = True
    relay_pins: List[int] = field(default_factory=lambda: [26, 20, 21])
    relay_active_high: bool = False
    relay_initial_value: bool = False

    # Debounce / phantom-press defense (button input only)
    hold_window_ms: int = 50
    sample_interval_ms: int = 5
    min_press_interval_ms: int = 300
    rapidfire_count: int = 3
    rapidfire_window_s: float = 1.0
    rapidfire_cooldown_s: float = 2.0
    gpiozero_bounce_time_s: float = 0.02

    # Audio / watchdog
    startup_delay_s: float = 5.0
    supervisor_poll_s: float = 0.1
    health_probe_interval_s: float = 30.0
    play_start_grace_s: float = 3.0
    max_reinit_attempts: int = 3
    on_audio_fault: str = "idle"  # "idle" | "resume"
    escalate_exit: bool = True
    force_mock_audio: bool = False

    # Web dashboard
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_auth_token: str = ""
    # Full songs are uploaded for server-side trimming, so the cap must fit
    # a whole track (§11.2), not just the stored clip
    upload_max_bytes: int = 30_000_000
    # Per-upload clip length is chosen in the dashboard within [clip_min_s,
    # clip_max_s]; clip_duration_s is the default the selector opens at (§11.2)
    clip_duration_s: float = 10.0
    clip_min_s: float = 10.0
    clip_max_s: float = 30.0
    loudness_target_lufs: float = -14.0

    # Analytics (SPECS §11.4): SQLite event store behind the analytics dashboard
    analytics_enabled: bool = True
    analytics_db_path: str = DEFAULT_PI_ANALYTICS_DB
    analytics_top_n: int = 10

    # Webhook (empty URL = disabled)
    webhook_url: str = ""
    webhook_timeout_s: float = 5.0

    # Logging
    log_file: str = DEFAULT_PI_LOG_FILE
    log_level: str = "INFO"
    log_ring_buffer_size: int = 500
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 3


def _coerce(value, target_type, key: str):
    """Coerce a raw (YAML or env-string) value to the dataclass field type."""
    if target_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    # List fields: accept YAML lists, or comma-separated strings from env vars
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(f"cannot coerce {value!r} for key {key}")
    if key == "relay_pins":
        return [int(v) for v in items]
    return [str(v) for v in items]


def load_config(path: Optional[str] = None) -> Config:
    cfg = Config()
    fields = {f.name: f for f in dataclasses.fields(Config)}

    if path:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"config file {path} must contain a YAML mapping")
        for key, value in data.items():
            if key not in fields:
                log.warning("Unknown config key in %s: %s (ignored)", path, key)
                continue
            base_type = _field_base_type(fields[key])
            setattr(cfg, key, _coerce(value, base_type, key))

    for key, f in fields.items():
        env_key = ENV_PREFIX + key.upper()
        if env_key in os.environ:
            setattr(cfg, key, _coerce(os.environ[env_key], _field_base_type(f), key))

    if cfg.on_audio_fault not in ("idle", "resume"):
        raise ValueError(f"on_audio_fault must be 'idle' or 'resume', got {cfg.on_audio_fault!r}")
    if cfg.mode not in ("auto", "real", "mock"):
        raise ValueError(f"mode must be 'auto', 'real' or 'mock', got {cfg.mode!r}")
    if not 0 < cfg.clip_min_s <= cfg.clip_max_s:
        raise ValueError(f"clip bounds must satisfy 0 < clip_min_s <= clip_max_s, "
                         f"got clip_min_s={cfg.clip_min_s}, clip_max_s={cfg.clip_max_s}")
    if not cfg.clip_min_s <= cfg.clip_duration_s <= cfg.clip_max_s:
        raise ValueError(f"clip_duration_s must be within [clip_min_s, clip_max_s], "
                         f"got clip_duration_s={cfg.clip_duration_s}")
    if len(cfg.relay_pins) != 3:
        log.warning("Expected 3 relay pins, got %d: %s", len(cfg.relay_pins), cfg.relay_pins)
    return cfg


def _field_base_type(f: dataclasses.Field):
    if f.type in ("int", int):
        return int
    if f.type in ("float", float):
        return float
    if f.type in ("bool", bool):
        return bool
    if f.type in ("str", str):
        return str
    return list


def resolve_music_folder(cfg: Config, mode: str) -> Path:
    """Mock mode uses MockMusics/ (project-relative); real mode the configured folder."""
    if mode == "mock":
        p = Path(cfg.mock_music_folder)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p
    return Path(cfg.music_folder)


def resolve_log_file(cfg: Config, mode: str) -> Path:
    """In mock mode, swap the default Pi log path for one inside the project."""
    if mode == "mock" and cfg.log_file == DEFAULT_PI_LOG_FILE:
        return PROJECT_ROOT / "saintantoine.log"
    return Path(cfg.log_file)


def resolve_analytics_db(cfg: Config, mode: str) -> Path:
    """In mock mode, swap the default Pi DB path for one inside the project."""
    if mode == "mock" and cfg.analytics_db_path == DEFAULT_PI_ANALYTICS_DB:
        return PROJECT_ROOT / "analytics.db"
    return Path(cfg.analytics_db_path)

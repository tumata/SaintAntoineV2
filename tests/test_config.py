"""Config loading: defaults, YAML overrides, env overrides, path resolution."""

import pytest

from saintantoine.config import (
    PROJECT_ROOT,
    Config,
    load_config,
    resolve_log_file,
    resolve_music_folder,
)


def test_defaults():
    cfg = load_config(None)
    assert cfg.button_pin == 22
    assert cfg.relay_pins == [26, 20, 21]
    assert cfg.relay_active_high is False
    assert cfg.web_port == 8080
    assert cfg.on_audio_fault == "idle"
    assert cfg.webhook_url == ""


def test_yaml_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "web_port: 9090\n"
        "relay_pins: [5, 6, 13]\n"
        "button_pull_up: true\n"
        "startup_delay_s: 0\n"
    )
    cfg = load_config(str(p))
    assert cfg.web_port == 9090
    assert cfg.relay_pins == [5, 6, 13]
    assert cfg.button_pull_up is True
    assert cfg.startup_delay_s == 0.0
    assert cfg.button_pin == 22  # untouched default


def test_unknown_key_ignored(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("not_a_real_key: 42\nweb_port: 9000\n")
    cfg = load_config(str(p))
    assert cfg.web_port == 9000


def test_env_overrides(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("web_port: 9090\n")
    monkeypatch.setenv("SAINTANTOINE_WEB_PORT", "7777")
    monkeypatch.setenv("SAINTANTOINE_RELAY_PINS", "5,6,13")
    monkeypatch.setenv("SAINTANTOINE_ESCALATE_EXIT", "false")
    cfg = load_config(str(p))
    assert cfg.web_port == 7777  # env beats yaml
    assert cfg.relay_pins == [5, 6, 13]
    assert cfg.escalate_exit is False


def test_invalid_fault_policy(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("on_audio_fault: explode\n")
    with pytest.raises(ValueError):
        load_config(str(p))


def test_music_folder_resolution():
    cfg = Config()
    assert str(resolve_music_folder(cfg, "real")) == "/home/pi/musique"
    mock_folder = resolve_music_folder(cfg, "mock")
    assert mock_folder == PROJECT_ROOT / "MockMusics"


def test_log_file_resolution():
    cfg = Config()
    assert str(resolve_log_file(cfg, "real")) == "/home/pi/saintantoine/log_script.txt"
    assert resolve_log_file(cfg, "mock") == PROJECT_ROOT / "saintantoine.log"
    cfg.log_file = "/tmp/custom.log"
    assert str(resolve_log_file(cfg, "mock")) == "/tmp/custom.log"

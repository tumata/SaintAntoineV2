"""ffmpeg wrapper (§11.2) with subprocess fully mocked — no ffmpeg needed."""

import subprocess

import pytest

from saintantoine import processing
from saintantoine.processing import ProcessingError

LOUDNORM_STDERR = """\
size=N/A time=00:00:10.00 bitrate=N/A speed= 234x
[Parsed_loudnorm_0 @ 0x7f8aa3c04d40]
{
\t"input_i" : "-23.10",
\t"input_tp" : "-5.50",
\t"input_lra" : "6.30",
\t"input_thresh" : "-33.40",
\t"output_i" : "-14.05",
\t"output_tp" : "-1.80",
\t"output_lra" : "5.90",
\t"output_thresh" : "-24.30",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.05"
}
"""


class FakeFfmpeg:
    """Stands in for processing._run: records commands, returns canned output."""

    def __init__(self, duration="100.0"):
        self.cmds = []
        self.duration = duration

    def __call__(self, cmd, what):
        self.cmds.append(cmd)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout=self.duration + "\n",
                                               stderr="")
        if cmd[-1] == "-":  # measurement pass writes to the null muxer
            return subprocess.CompletedProcess(cmd, 0, stdout="",
                                               stderr=LOUDNORM_STDERR)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    fake = FakeFfmpeg()
    monkeypatch.setattr(processing, "_run", fake)
    return fake


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_ffmpeg_available(monkeypatch):
    monkeypatch.setattr(processing.shutil, "which", lambda name: "/usr/bin/" + name)
    assert processing.ffmpeg_available()
    monkeypatch.setattr(processing.shutil, "which",
                        lambda name: None if name == "ffmpeg" else "/usr/bin/" + name)
    assert not processing.ffmpeg_available()


def test_probe_duration(fake_ffmpeg, tmp_path):
    assert processing.probe_duration(tmp_path / "a.mp3") == 100.0
    fake_ffmpeg.duration = "garbage"
    with pytest.raises(ProcessingError, match="unreadable duration"):
        processing.probe_duration(tmp_path / "a.mp3")


def test_measure_loudness_parses_json(fake_ffmpeg, tmp_path):
    stats = processing.measure_loudness(tmp_path / "a.mp3", -14.0,
                                        start_s=30.0, duration_s=10.0)
    assert stats["input_i"] == "-23.10"
    assert stats["target_offset"] == "0.05"
    [cmd] = fake_ffmpeg.cmds
    assert _flag(cmd, "-ss") == "30.000"
    assert _flag(cmd, "-t") == "10.000"
    assert "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json" in _flag(cmd, "-af")


def test_measure_loudness_without_stats_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(processing, "_run",
                        lambda cmd, what: subprocess.CompletedProcess(cmd, 0, "", "no json here"))
    with pytest.raises(ProcessingError, match="no loudnorm stats"):
        processing.measure_loudness(tmp_path / "a.mp3", -14.0)


def test_process_clip(fake_ffmpeg, tmp_path):
    src, dest = tmp_path / "in.mp3", tmp_path / "out.mp3"
    processing.process_clip(src, dest, start_s=30.0, clip_s=10.0, target_lufs=-14.0)
    probe, measure, encode = fake_ffmpeg.cmds
    assert probe[0] == "ffprobe"
    # Both passes must trim the same segment
    for cmd in (measure, encode):
        assert _flag(cmd, "-ss") == "30.000"
        assert _flag(cmd, "-t") == "10.000"
    af = _flag(encode, "-af")
    assert "measured_I=-23.10" in af and "offset=0.05" in af and "linear=true" in af
    assert "afade=t=in:d=0.3" in af and "afade=t=out:st=9.700:d=0.3" in af
    assert _flag(encode, "-codec:a") == "libmp3lame"
    assert _flag(encode, "-f") == "mp3"
    assert dest.exists()
    assert not list(tmp_path.glob("*.part"))


def test_process_clip_clamps_start(fake_ffmpeg, tmp_path):
    fake_ffmpeg.duration = "12.0"
    processing.process_clip(tmp_path / "in.mp3", tmp_path / "out.mp3",
                            start_s=50.0, clip_s=10.0, target_lufs=-14.0)
    _, measure, _ = fake_ffmpeg.cmds
    assert _flag(measure, "-ss") == "2.000"  # clamped so the window fits


def test_process_clip_short_song_kept_whole(fake_ffmpeg, tmp_path):
    fake_ffmpeg.duration = "6.0"
    processing.process_clip(tmp_path / "in.mp3", tmp_path / "out.mp3",
                            start_s=3.0, clip_s=10.0, target_lufs=-14.0)
    _, measure, encode = fake_ffmpeg.cmds
    assert _flag(measure, "-ss") == "0.000"
    assert _flag(measure, "-t") == "6.000"
    assert "afade=t=out:st=5.700" in _flag(encode, "-af")


def test_process_clip_tiny_song_no_fades(fake_ffmpeg, tmp_path):
    fake_ffmpeg.duration = "0.4"
    processing.process_clip(tmp_path / "in.mp3", tmp_path / "out.mp3",
                            start_s=0.0, clip_s=10.0, target_lufs=-14.0)
    assert "afade" not in _flag(fake_ffmpeg.cmds[-1], "-af")


def test_normalize_in_place(fake_ffmpeg, tmp_path):
    path = tmp_path / "quiet.mp3"
    path.write_bytes(b"old")
    processing.normalize_in_place(path, -14.0)
    measure, encode = fake_ffmpeg.cmds
    assert "-ss" not in measure and "-t" not in measure  # whole file
    assert "afade" not in _flag(encode, "-af")
    assert _flag(encode, "-f") == "mp3"
    assert path.exists()
    assert not list(tmp_path.glob("*.part"))


def test_run_reports_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        processing.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom"))
    with pytest.raises(ProcessingError, match="exited with code 1"):
        processing.probe_duration(tmp_path / "a.mp3")


def test_run_reports_missing_binary(monkeypatch, tmp_path):
    def raise_missing(*a, **k):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(processing.subprocess, "run", raise_missing)
    with pytest.raises(ProcessingError, match="not installed"):
        processing.probe_duration(tmp_path / "a.mp3")


def test_encode_failure_cleans_temp_and_keeps_original(monkeypatch, tmp_path):
    path = tmp_path / "quiet.mp3"
    path.write_bytes(b"original")

    def run(cmd, what):
        if cmd[-1] == "-":
            return subprocess.CompletedProcess(cmd, 0, "", LOUDNORM_STDERR)
        raise ProcessingError("encode blew up")

    monkeypatch.setattr(processing, "_run", run)
    with pytest.raises(ProcessingError, match="encode blew up"):
        processing.normalize_in_place(path, -14.0)
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.part"))

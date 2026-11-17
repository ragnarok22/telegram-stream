"""The browser encoder supplies a bounded, compatible H.264 stream."""

from pathlib import Path

import pytest

from telegram_stream.errors import FatalError
from telegram_stream.web import WebSettings, capture_command


def test_web_encoder_bounds_bitrate_and_preserves_output_gop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAGE_URL", "https://example.invalid")
    monkeypatch.setenv("WEB_VIDEO_BITRATE", "1500k")
    monkeypatch.setenv("WEB_FPS", "30")
    monkeypatch.setenv("WEB_CAPTURE_FPS", "15")
    settings = WebSettings.from_env()
    command = capture_command(settings, tmp_path)
    for flag, expected in [
        ("-b:v", "1500000"),
        ("-maxrate", "1500000"),
        ("-bufsize", "3000000"),
        ("-profile:v", "main"),
        ("-g", "30"),
        ("-keyint_min", "30"),
    ]:
        assert command[command.index(flag) + 1] == expected
    assert "-crf" not in command
    assert command[command.index("-vf") + 1].endswith("fps=30")


@pytest.mark.parametrize("bitrate", ["0", "-100k", "fast", "2.5m"])
def test_web_encoder_rejects_invalid_bitrate(monkeypatch: pytest.MonkeyPatch, bitrate: str) -> None:
    monkeypatch.setenv("PAGE_URL", "https://example.invalid")
    monkeypatch.setenv("WEB_VIDEO_BITRATE", bitrate)
    with pytest.raises(FatalError, match="WEB_VIDEO_BITRATE"):
        WebSettings.from_env()

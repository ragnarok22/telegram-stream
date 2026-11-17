"""Independent X11 capture rate and geometry with conservative defaults."""

import os
from pathlib import Path

import pytest

from telegram_stream.errors import FatalError
from telegram_stream.web import WebSettings, capture_command, chromium_command


@pytest.fixture(autouse=True)
def web_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAGE_URL", "https://example.invalid")
    for name in tuple(os.environ):
        if name.startswith("WEB_"):
            monkeypatch.delenv(name)


def test_default_capture_keeps_supersampling_and_output_rate(tmp_path: Path) -> None:
    settings = WebSettings.from_env()
    assert settings.capture_scale == 2
    assert settings.effective_capture_fps == settings.fps == 30
    assert (settings.capture_width, settings.capture_height) == (1080, 1920)
    command = capture_command(settings, tmp_path)
    assert command[command.index("-framerate") + 1] == "30"
    assert command[command.index("-vf") + 1] == "scale=720:1280,setsar=1"


@pytest.mark.parametrize("fps", [1, 24, 60])
def test_unspecified_capture_rate_follows_output_rate(
    monkeypatch: pytest.MonkeyPatch, fps: int
) -> None:
    settings = WebSettings("https://example.invalid", fps=fps)
    assert settings.capture_fps is None
    assert settings.effective_capture_fps == fps
    monkeypatch.setenv("WEB_FPS", str(fps))
    assert WebSettings.from_env().effective_capture_fps == fps


def test_native_capture_at_15_fps_retains_30_fps_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEB_CAPTURE_FPS", "15")
    monkeypatch.setenv("WEB_CAPTURE_SCALE", "1")
    settings = WebSettings.from_env()
    assert settings.capture_fps == settings.effective_capture_fps == 15
    assert settings.fps == 30
    command = capture_command(settings, tmp_path)
    assert command[command.index("-video_size") + 1] == "720x1280"
    assert command[command.index("-framerate") + 1] == "15"
    assert command[command.index("-vf") + 1] == "scale=720:1280,setsar=1,fps=30"
    assert command[command.index("-g") + 1] == "30"
    assert command[command.index("-keyint_min") + 1] == "30"
    chrome = chromium_command(settings)
    assert "--force-device-scale-factor=1" in chrome
    assert "--window-size=720,1280" in chrome


@pytest.mark.parametrize("fps", [1, 60])
def test_explicit_capture_rate_accepts_inclusive_bounds(
    monkeypatch: pytest.MonkeyPatch, fps: int
) -> None:
    monkeypatch.setenv("WEB_FPS", str(fps))
    monkeypatch.setenv("WEB_CAPTURE_FPS", str(fps))
    assert WebSettings.from_env().effective_capture_fps == fps


@pytest.mark.parametrize("fps", ["-1", "0", "61", "", "1.5", "fifteen"])
def test_capture_rate_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, fps: str) -> None:
    monkeypatch.setenv("WEB_CAPTURE_FPS", fps)
    with pytest.raises(FatalError, match="WEB_CAPTURE_FPS"):
        WebSettings.from_env()


def test_capture_rate_cannot_exceed_output_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_FPS", "15")
    monkeypatch.setenv("WEB_CAPTURE_FPS", "16")
    with pytest.raises(FatalError, match="WEB_CAPTURE_FPS must not exceed WEB_FPS"):
        WebSettings.from_env()


@pytest.mark.parametrize("scale", ["-1", "0", "3", "", "1.5", "native"])
def test_capture_scale_rejects_values_other_than_one_or_two(
    monkeypatch: pytest.MonkeyPatch, scale: str
) -> None:
    monkeypatch.setenv("WEB_CAPTURE_SCALE", scale)
    with pytest.raises(FatalError, match="WEB_CAPTURE_SCALE"):
        WebSettings.from_env()


@pytest.mark.parametrize(
    ("width", "height", "scale", "capture_width", "capture_height"),
    [
        (720, 1280, 1, 720, 1280),
        (720, 1280, 2, 1080, 1920),
        (128, 128, 1, 540, 540),
        (128, 128, 2, 1080, 1080),
        (128, 194, 1, 540, 818),
        (722, 1280, 2, 1080, 1914),
        (1920, 1080, 1, 1920, 1080),
        (1920, 1080, 2, 1920, 1080),
        (4096, 4096, 1, 4096, 4096),
        (4096, 4096, 2, 4096, 4096),
    ],
)
def test_capture_geometry_preserves_aspect_ratio_and_minimum_css_viewport(
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    height: int,
    scale: int,
    capture_width: int,
    capture_height: int,
) -> None:
    monkeypatch.setenv("WEB_WIDTH", str(width))
    monkeypatch.setenv("WEB_HEIGHT", str(height))
    monkeypatch.setenv("WEB_CAPTURE_SCALE", str(scale))
    settings = WebSettings.from_env()
    assert (settings.capture_width, settings.capture_height) == (capture_width, capture_height)
    assert capture_width % 2 == capture_height % 2 == 0
    assert capture_width // scale >= 540
    assert abs(capture_height - capture_width * height / width) <= 1
    chrome = chromium_command(settings)
    assert f"--force-device-scale-factor={scale}" in chrome
    assert f"--window-size={capture_width // scale},{capture_height // scale}" in chrome


@pytest.mark.parametrize(("scale", "width"), [(1, 270), (2, 540)])
def test_capture_height_accepts_8192_pixels_but_rejects_larger_aspect_ratios(
    monkeypatch: pytest.MonkeyPatch, scale: int, width: int
) -> None:
    monkeypatch.setenv("WEB_WIDTH", str(width))
    monkeypatch.setenv("WEB_HEIGHT", "4096")
    monkeypatch.setenv("WEB_CAPTURE_SCALE", str(scale))
    assert WebSettings.from_env().capture_height == 8192
    monkeypatch.setenv("WEB_WIDTH", str(width - 2))
    with pytest.raises(FatalError, match="reasonable aspect ratio"):
        WebSettings.from_env()


@pytest.mark.parametrize("dimension", ["WEB_WIDTH", "WEB_HEIGHT"])
@pytest.mark.parametrize("scale", [1, 2])
def test_capture_still_requires_even_output_dimensions(
    monkeypatch: pytest.MonkeyPatch, dimension: str, scale: int
) -> None:
    monkeypatch.setenv(dimension, "721")
    monkeypatch.setenv("WEB_CAPTURE_SCALE", str(scale))
    with pytest.raises(FatalError, match="even"):
        WebSettings.from_env()

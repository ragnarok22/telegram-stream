"""Resource optimization settings remain explicit and bounded."""

import pytest

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError


def configuration(**overrides: str) -> Settings:
    return Settings.from_env(
        {
            "TELEGRAM_API_ID": "123",
            "TELEGRAM_API_HASH": "a" * 32,
            "TELEGRAM_CHANNEL": "@test",
            "SOURCE": "https://www.youtube.com/watch?v=test",
            **overrides,
        }
    )


def test_youtube_defaults_cap_formats_and_copy_is_opt_in() -> None:
    settings = configuration()
    assert (settings.youtube_max_height, settings.youtube_max_fps) == (720, 30)
    assert not settings.web_video_copy


def test_optimization_settings_can_be_overridden() -> None:
    settings = configuration(YOUTUBE_MAX_HEIGHT="1080", YOUTUBE_MAX_FPS="60", WEB_VIDEO_COPY="true")
    assert (settings.youtube_max_height, settings.youtube_max_fps) == (1080, 60)
    assert settings.web_video_copy


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("YOUTUBE_MAX_HEIGHT", "0"),
        ("YOUTUBE_MAX_HEIGHT", "4321"),
        ("YOUTUBE_MAX_FPS", "0"),
        ("YOUTUBE_MAX_FPS", "121"),
        ("YOUTUBE_MAX_FPS", "many"),
        ("WEB_VIDEO_COPY", "sometimes"),
    ],
)
def test_invalid_optimization_settings_are_rejected(key: str, value: str) -> None:
    with pytest.raises(FatalError, match=key):
        configuration(**{key: value})

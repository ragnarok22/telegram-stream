"""Exercise yt-dlp's real selector with synthetic metadata, without network or cookies."""

import asyncio
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError

from telegram_stream.config import Settings
from telegram_stream.sources import SourceResolver, _youtube_format_selector


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Synthetic format selection must not make network requests")

    monkeypatch.setattr(YoutubeDL, "urlopen", forbidden)


def media_format(
    format_id: str,
    *,
    height: int | None = 720,
    fps: float | None = 30,
    protocol: str = "https",
    video: bool = True,
    audio: bool = True,
) -> dict[str, object]:
    return {
        "format_id": format_id,
        "url": f"https://media.example.invalid/{format_id}",
        "ext": "mp4" if video else "m4a",
        "protocol": protocol,
        "vcodec": "avc1.4d401f" if video else "none",
        "acodec": "mp4a.40.2" if audio else "none",
        "height": height if video else None,
        "fps": fps if video else None,
    }


def select_ids(
    formats: list[dict[str, object]], *, max_height: int = 720, max_fps: int = 30
) -> list[str]:
    with YoutubeDL(
        {
            "format": _youtube_format_selector(max_height, max_fps),
            "quiet": True,
            "no_warnings": True,
            "check_formats": False,
            "cachedir": False,
        }
    ) as downloader:
        info = downloader.process_ie_result(
            {
                "id": "synthetic-live",
                "title": "Synthetic live",
                "extractor": "synthetic",
                "extractor_key": "Synthetic",
                "live_status": "is_live",
                "formats": formats,
            },
            download=False,
        )
    return [media["format_id"] for media in info.get("requested_formats") or [info]]


def test_hls_combined_is_preferred_within_both_caps() -> None:
    assert select_ids(
        [
            media_format("hls-low", protocol="m3u8_native", height=360),
            media_format("hls", protocol="m3u8_native", height=480),
            media_format("hls-4k", protocol="m3u8_native", height=2160),
            media_format("hls-60fps", protocol="m3u8_native", fps=60),
            media_format("combined"),
            media_format("video", audio=False),
            media_format("audio", video=False),
        ]
    ) == ["hls"]


def test_combined_fallback_keeps_caps_and_precedes_separate_streams() -> None:
    assert select_ids(
        [
            media_format("hls-4k", protocol="m3u8_native", height=2160),
            media_format("hls-60fps", protocol="m3u8_native", fps=60),
            media_format("combined-low", height=480),
            media_format("combined"),
            media_format("combined-4k", height=2160),
            media_format("combined-60fps", fps=60),
            media_format("video", audio=False),
            media_format("audio", video=False),
        ]
    ) == ["combined"]


def test_video_audio_fallback_keeps_caps_without_filtering_audio_metadata() -> None:
    assert select_ids(
        [
            media_format("hls-4k", protocol="m3u8_native", height=2160),
            media_format("combined-60fps", fps=60),
            media_format("video-low", height=480, audio=False),
            media_format("video", audio=False),
            media_format("video-4k", height=2160, audio=False),
            media_format("video-60fps", fps=60, audio=False),
            media_format("audio", video=False),
        ]
    ) == ["video", "audio"]


@pytest.mark.parametrize(
    "protocol,audio",
    [("m3u8_native", True), ("https", True), ("https", False)],
    ids=["hls", "combined", "video-audio"],
)
@pytest.mark.parametrize("height,fps", [(2160, 30), (720, 60), (2160, 60)])
def test_no_fallback_reopens_over_budget_formats(
    protocol: str, audio: bool, height: int, fps: int
) -> None:
    with pytest.raises(ExtractorError, match="Requested format is not available"):
        select_ids(
            [
                media_format("over-budget", height=height, fps=fps, protocol=protocol, audio=audio),
                media_format("audio", video=False),
            ]
        )


@pytest.mark.parametrize(
    "protocol,audio",
    [("m3u8_native", True), ("https", True), ("https", False)],
    ids=["hls", "combined", "video-audio"],
)
@pytest.mark.parametrize("field", ["height", "fps"])
@pytest.mark.parametrize("absent", [False, True], ids=["null", "absent"])
def test_unknown_video_limits_are_rejected_in_every_path(
    protocol: str, audio: bool, field: str, absent: bool
) -> None:
    unknown = media_format("unknown", protocol=protocol, audio=audio)
    if absent:
        unknown.pop(field)
    else:
        unknown[field] = None
    with pytest.raises(ExtractorError, match="Requested format is not available"):
        select_ids([unknown, media_format("audio", video=False)])


@pytest.mark.parametrize(
    "protocol,audio",
    [("m3u8_native", True), ("https", True), ("https", False)],
    ids=["hls", "combined", "video-audio"],
)
@pytest.mark.parametrize("max_height,max_fps", [(480, 24), (1080, 60), (4320, 120)])
def test_custom_limits_and_inclusive_boundaries_in_every_path(
    protocol: str, audio: bool, max_height: int, max_fps: int
) -> None:
    expected = ["at-limit"] if audio else ["at-limit", "audio"]
    assert (
        select_ids(
            [
                media_format(
                    "at-limit", height=max_height, fps=max_fps, protocol=protocol, audio=audio
                ),
                media_format(
                    "too-tall", height=max_height + 1, fps=max_fps, protocol=protocol, audio=audio
                ),
                media_format(
                    "too-fast",
                    height=max_height,
                    fps=max_fps + 0.001,
                    protocol=protocol,
                    audio=audio,
                ),
                media_format("audio", video=False),
            ],
            max_height=max_height,
            max_fps=max_fps,
        )
        == expected
    )


def test_fractional_fps_below_limit_is_accepted() -> None:
    assert select_ids(
        [
            media_format("ntsc", fps=29.97),
            media_format("too-fast", fps=30.001),
        ]
    ) == ["ntsc"]


def test_resolver_passes_configured_caps_on_every_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    async def fake_capture(command: list[str], timeout: float, **kwargs: object) -> bytes:
        commands.append(command)
        return b'{"live_status":"is_live","url":"https://media.example.invalid/live.m3u8"}'

    monkeypatch.setattr("telegram_stream.sources.capture", fake_capture)
    resolver = SourceResolver(
        Settings(
            123,
            "a" * 32,
            source="https://youtu.be/synthetic",
            data_dir=tmp_path,
            youtube_max_height=480,
            youtube_max_fps=24,
            extractor_args="youtube:player_client=web",
        )
    )

    async def scenario() -> None:
        await resolver._youtube()
        await resolver._youtube()

    asyncio.run(scenario())
    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--format") + 1] == _youtube_format_selector(480, 24)
        assert command[command.index("--extractor-args") + 1] == "youtube:player_client=web"
        assert "--ignore-config" in command
        assert "--cookies" not in command
        assert command[-2:] == ["--", "https://youtu.be/synthetic"]

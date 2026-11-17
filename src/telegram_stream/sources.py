"""Resolve fresh input URLs and inspect media without blocking the event loop."""

import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, OpenerDirector, Request

from telegram_stream.config import Settings
from telegram_stream.cookies import (
    normalize_document,
    read_document,
    write_private,
    youtube_diagnostic,
)
from telegram_stream.errors import FatalError, TemporaryError
from telegram_stream.logging import redactor
from telegram_stream.process import capture


@dataclass(frozen=True)
class MediaInput:
    location: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)

    def options(self, timeout: int, *, reconnect: bool = False) -> list[str]:
        if urlsplit(self.location).scheme not in {"http", "https"}:
            return []
        options = ["-rw_timeout", str(timeout * 1_000_000)]
        if reconnect:
            options += [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_on_network_error",
                "1",
                "-reconnect_on_http_error",
                "5xx",
                "-reconnect_delay_max",
                "5",
            ]
        if self.headers:
            for key, value in self.headers.items():
                if not re.fullmatch(r"[A-Za-z0-9-]+", key) or "\r" in value or "\n" in value:
                    raise FatalError("Extractor returned unsafe HTTP headers")
            options += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())]
        return options


@dataclass(frozen=True)
class ResolvedSource:
    inputs: tuple[MediaInput, ...]
    video_index: int
    audio_index: int | None
    local: bool = False
    video_copy: bool = False


def _positive_integer(value: object) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        number = int(value)
        return number if number > 0 else None
    return None


def _ratio(value: object) -> Fraction | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}[/:][0-9]{1,10}", value):
        return None
    try:
        return Fraction(value.replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return None


def _copy_bitrate_compatible(stream: dict[str, object], limit: int) -> bool:
    for name in ("bit_rate", "max_bit_rate"):
        value = stream.get(name)
        # MPEG-TS/HLS commonly omits these; the sidecar contract must bound VBV.
        if type(value) is bool:
            return False
        if value not in (None, "N/A", "0", 0):
            bitrate = _positive_integer(value)
            if bitrate is None or bitrate > limit:
                return False
    return True


def _copy_video_compatible(settings: Settings, stream: dict[str, object]) -> bool:
    """Only accept the first mapped video stream, never container/URL hints."""
    field_order = stream.get("field_order")
    if field_order is None and stream.get("profile") in ("Baseline", "Constrained Baseline"):
        # Baseline cannot use interlaced coding; HLS often omits field_order.
        field_order = "progressive"
    if (
        stream.get("codec_name") != "h264"
        or stream.get("width") != settings.width
        or stream.get("height") != settings.height
        or stream.get("pix_fmt") != "yuv420p"
        or stream.get("profile") not in ("Baseline", "Constrained Baseline", "Main")
        or field_order != "progressive"
        or _ratio(stream.get("sample_aspect_ratio")) != 1
        or _ratio(stream.get("r_frame_rate")) != settings.fps
        or _ratio(stream.get("avg_frame_rate")) != settings.fps
    ):
        return False
    # H.264 levels through 4.2: max frame size and macroblocks/second.
    limits = {
        10: (99, 1485),
        11: (396, 3000),
        12: (396, 6000),
        13: (396, 11880),
        20: (396, 11880),
        21: (792, 19800),
        22: (1620, 20250),
        30: (1620, 40500),
        31: (3600, 108000),
        32: (5120, 216000),
        40: (8192, 245760),
        41: (8192, 245760),
        42: (8704, 522240),
    }
    level = _positive_integer(stream.get("level"))
    if level is None or level not in limits:
        return False
    max_frame, max_rate = limits[level]
    macroblocks = ((settings.width + 15) // 16) * ((settings.height + 15) // 16)
    if macroblocks > max_frame or macroblocks * settings.fps > max_rate:
        return False
    if not _copy_bitrate_compatible(stream, settings.video_bitrate):
        return False
    tags = stream.get("tags", {})
    side_data = stream.get("side_data_list", [])
    if not isinstance(tags, dict) or not isinstance(side_data, list):
        return False
    rotations = [tags.get("rotate", "0")]
    for item in side_data:
        if not isinstance(item, dict):
            return False
        rotations.append(item.get("rotation", 0))
    # Stream copy cannot apply FFmpeg's automatic display-matrix rotation.
    return all(str(rotation) in {"0", "0.0", "-0", "-0.0"} for rotation in rotations)


def _read_web_copy_contract(location: str, timeout: int) -> object:
    """Read bounded, uncached metadata directly, without proxies or redirects."""
    endpoint = urlsplit(location)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username:
        return None
    endpoint = endpoint._replace(path="/stream-info", fragment="")
    opener = OpenerDirector()
    opener.add_handler(HTTPHandler())
    opener.add_handler(HTTPSHandler())
    request = Request(
        endpoint.geturl(), headers={"Accept": "application/json", "Cache-Control": "no-cache"}
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = response.read(4097)
        if len(payload) > 4096:
            return None
        info = json.loads(payload)
        return info if isinstance(info, dict) else None
    except (OSError, ValueError, HTTPException, RecursionError):
        return None


async def _fetch_web_copy_contract(location: str, timeout: int) -> object:
    # A socket timeout alone cannot bound DNS, headers or a slowly trickled body.
    # Reuse subprocess supervision so timeout/cancellation closes the entire reader.
    output = await capture(
        [
            sys.executable,
            "-c",
            "import json, sys; "
            "from telegram_stream.sources import _read_web_copy_contract; "
            "print(json.dumps(_read_web_copy_contract(sys.argv[1], int(sys.argv[2]))))",
            location,
            str(timeout),
        ],
        timeout,
    )
    return json.loads(output)


def _copy_contract_compatible(settings: Settings, info: object, stream: dict[str, object]) -> bool:
    """Version 1 describes the controlled sidecar's bounded x264 encoder."""
    if not isinstance(info, dict):
        return False
    if (
        type(info.get("version")) is not int
        or info["version"] != 1
        or info.get("ready") is not True
        or info.get("codec") != "h264"
        or info.get("profile") != "main"
        or info.get("pix_fmt") != "yuv420p"
        or _ratio(info.get("sample_aspect_ratio")) != 1
    ):
        return False
    for name, expected in (
        ("width", settings.width),
        ("height", settings.height),
        ("fps", settings.fps),
    ):
        if type(info.get(name)) is not int or info[name] != expected:
            return False
    bitrate = info.get("video_bitrate")
    maxrate = info.get("maxrate")
    gop = info.get("gop")
    return (
        type(bitrate) is int
        and 0 < bitrate <= settings.video_bitrate
        and type(maxrate) is int
        and maxrate == bitrate
        and _copy_bitrate_compatible(stream, maxrate)
        and type(gop) is int
        and 0 < gop <= settings.fps * 2
    )


def _youtube_format_selector(max_height: int, max_fps: int) -> str:
    # Do not use yt-dlp's <=? comparisons: missing height/FPS cannot prove that
    # a format fits the input budget. Every video fallback must keep both caps.
    limits = f"[height<={max_height}][fps<={max_fps}]"
    combined = "best[vcodec!=none][acodec!=none]"
    return f"{combined}[protocol^=m3u8]{limits}/{combined}{limits}/bestvideo{limits}+bestaudio"


class SourceResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _youtube(self) -> tuple[MediaInput, ...]:
        command = [
            "yt-dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--no-cache-dir",
            "--skip-download",
            "--dump-single-json",
            "--js-runtimes",
            "deno",
            "--format",
            _youtube_format_selector(
                self.settings.youtube_max_height, self.settings.youtube_max_fps
            ),
        ]
        if self.settings.extractor_args:
            command += ["--extractor-args", self.settings.extractor_args]
        # yt-dlp may update its cookie jar. Never write back to a mounted secret.
        with tempfile.TemporaryDirectory(prefix="telegram-stream-") as directory:
            cookie_file = self.settings.youtube_cookie_file
            if cookie_file is not None:
                if not cookie_file.is_file():
                    raise FatalError("YOUTUBE_COOKIES_FILE is not a readable file")
                cookies = Path(directory) / "cookies.txt"
                write_private(cookies, normalize_document(read_document(cookie_file)))
                command += ["--cookies", str(cookies)]
            output = await capture(
                [*command, "--", self.settings.source],
                self.settings.source_timeout,
                diagnostic=youtube_diagnostic,
            )
        try:
            info = json.loads(output)
            if info.get("live_status") != "is_live":
                raise TemporaryError("YouTube source is not currently live")
            formats = info.get("requested_formats") or [info]
            inputs = []
            for media in formats:
                location = media["url"]
                if urlsplit(location).scheme not in {"http", "https"}:
                    raise TemporaryError("YouTube returned an unsupported media protocol")
                headers = {**info.get("http_headers", {}), **media.get("http_headers", {})}
                inputs.append(MediaInput(location, headers))
            if not 1 <= len(inputs) <= 2:
                raise TemporaryError("Unsupported YouTube format selection")
            return tuple(inputs)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TemporaryError("Invalid yt-dlp media metadata") from None

    async def resolve(self) -> ResolvedSource:
        local = self.settings.kind == "file"
        copy_requested = self.settings.kind == "web" and self.settings.web_video_copy
        if self.settings.kind == "youtube":
            inputs = await self._youtube()
        elif local:
            path = Path(self.settings.source).expanduser().resolve()
            if not path.is_file():
                raise TemporaryError("Local source file is not available")
            inputs = (MediaInput(str(path)),)
        elif self.settings.kind == "web":
            # The isolated browser sidecar renders HTML into a private HLS feed.
            # Never give the HTML page URL directly to ffprobe/FFmpeg.
            inputs = (MediaInput(self.settings.web_stream_url),)
        else:
            inputs = (MediaInput(self.settings.source),)
        video_index = None
        audio_index = None
        video_metadata: dict[str, object] = {}
        entries = "stream=codec_type"
        if copy_requested:
            entries += (
                ",codec_name,width,height,pix_fmt,profile,level,field_order,"
                "sample_aspect_ratio,r_frame_rate,avg_frame_rate,bit_rate,max_bit_rate"
                ":stream_tags=rotate:stream_side_data=rotation"
            )
        for index, media in enumerate(inputs):
            # URLs are redacted generically; do not retain every expiring URL forever.
            redactor.add(*media.headers.values())
            output = await capture(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    *media.options(self.settings.rpc_timeout),
                    "-show_entries",
                    entries,
                    "-of",
                    "json",
                    "-i",
                    media.location,
                ],
                self.settings.source_timeout,
            )
            try:
                metadata = json.loads(output)["streams"]
                if not isinstance(metadata, list) or any(
                    not isinstance(stream, dict) or not isinstance(stream.get("codec_type"), str)
                    for stream in metadata
                ):
                    raise ValueError
                streams = {stream["codec_type"] for stream in metadata}
            except (ValueError, KeyError, TypeError):
                raise TemporaryError("Invalid ffprobe media metadata") from None
            if "video" in streams and video_index is None:
                video_index = index
                video_metadata = next(
                    stream for stream in metadata if stream["codec_type"] == "video"
                )
            if "audio" in streams and audio_index is None:
                audio_index = index
        if video_index is None:
            raise TemporaryError("Source has no video stream")
        video_copy = False
        if copy_requested and _copy_video_compatible(self.settings, video_metadata):
            try:
                timeout = min(3, self.settings.rpc_timeout)
                info = await _fetch_web_copy_contract(self.settings.web_stream_url, timeout)
                video_copy = _copy_contract_compatible(self.settings, info, video_metadata)
            except (TemporaryError, OSError, TimeoutError, ValueError):
                pass
        return ResolvedSource(inputs, video_index, audio_index, local, video_copy=video_copy)

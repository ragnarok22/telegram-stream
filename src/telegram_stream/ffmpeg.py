"""FFmpeg command construction and media-progress based supervision."""

import asyncio
import logging
import time

from telegram_stream.config import Settings
from telegram_stream.errors import TemporaryError
from telegram_stream.logging import event, redactor
from telegram_stream.process import spawn, terminate
from telegram_stream.sources import ResolvedSource

logger = logging.getLogger(__name__)


def build_command(settings: Settings, source: ResolvedSource, destination: str) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats"]
    for media in source.inputs:
        if source.local:
            command += ["-re", "-stream_loop", "-1"]
        command += [
            *media.options(settings.rpc_timeout, reconnect=True),
            "-i",
            media.location,
        ]
    audio_index = source.audio_index
    if audio_index is None:
        audio_index = len(source.inputs)
        command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    command += [
        "-map",
        f"{source.video_index}:v:0",
        "-map",
        f"{audio_index}:a:0",
    ]
    if (
        settings.kind == "web"
        and settings.web_video_copy
        and source.video_copy
        and not source.local
    ):
        command += ["-c:v", "copy"]
    else:
        video_filter = (
            f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease"
            ":force_divisible_by=2,"
            f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={settings.fps}"
        )
        command += [
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            settings.preset,
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "main",
            "-b:v",
            str(settings.video_bitrate),
            "-maxrate",
            str(settings.video_bitrate),
            "-bufsize",
            str(settings.video_bitrate * 2),
            "-g",
            str(settings.fps * 2),
            "-keyint_min",
            str(settings.fps * 2),
            "-sc_threshold",
            "0",
        ]
    command += [
        "-af",
        "aresample=async=1:first_pts=0",
        "-c:a",
        "aac",
        "-b:a",
        str(settings.audio_bitrate),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-shortest",
        "-rw_timeout",
        str(settings.rpc_timeout * 1_000_000),
        "-flvflags",
        "no_duration_filesize",
        "-f",
        "flv",
        "-progress",
        "pipe:1",
        destination,
    ]
    return command


class FFmpeg:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.process: asyncio.subprocess.Process | None = None
        self.readers: list[asyncio.Task[None]] = []
        self.started_at = 0.0
        self.progress_at = 0.0
        self.media_time = -1
        self.frames = -1

    async def start(self, source: ResolvedSource, destination: str) -> None:
        if self.process is not None:
            raise RuntimeError("FFmpeg must be stopped before starting another publisher")
        command = build_command(self.settings, source, destination)
        try:
            self.process = await spawn(command)
        except FileNotFoundError:
            raise TemporaryError("FFmpeg executable is not installed") from None
        self.started_at = time.monotonic()
        self.progress_at = 0.0
        self.media_time = -1
        self.frames = -1
        self.readers = [
            asyncio.create_task(self._progress()),
            asyncio.create_task(self._stderr()),
        ]
        event(
            logger,
            "ffmpeg_started",
            pid=self.process.pid,
            video_mode="copy" if command[command.index("-c:v") + 1] == "copy" else "transcode",
        )

    async def _progress(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        reported_frame = -1
        async for raw in self.process.stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key == "frame":
                try:
                    reported_frame = int(value)
                except ValueError:
                    continue
            elif key == "out_time_us":
                try:
                    media_time = int(value)
                except ValueError:
                    continue
                if (
                    media_time > self.media_time
                    and media_time > 0
                    and reported_frame > self.frames
                    and reported_frame > 0
                ):
                    self.media_time = media_time
                    self.frames = reported_frame
                    self.progress_at = time.monotonic()

    async def _stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        pending = b""
        oversized = False
        while chunk := await self.process.stderr.read(4096):
            for index, part in enumerate(chunk.split(b"\n")):
                if index:
                    if oversized:
                        detail = "Oversized diagnostic omitted"
                    else:
                        detail = redactor.clean(pending.decode(errors="replace").strip())[:2048]
                    if detail:
                        event(logger, "ffmpeg_diagnostic", detail=detail)
                    pending, oversized = b"", False
                if not oversized:
                    pending += part
                    if len(pending) > 262144:
                        pending, oversized = b"", True
        if pending and not oversized:
            detail = redactor.clean(pending.decode(errors="replace").strip())[:2048]
            if detail:
                event(logger, "ffmpeg_diagnostic", detail=detail)

    def check(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is not None:
            raise TemporaryError(f"FFmpeg exited (code {self.process.returncode})")
        if any(task.done() for task in self.readers):
            raise TemporaryError("FFmpeg diagnostic or progress pipe closed")
        now = time.monotonic()
        if not self.progress_at:
            if now - self.started_at > self.settings.startup_timeout:
                raise TemporaryError("FFmpeg did not produce media before STARTUP_TIMEOUT")
        elif now - self.progress_at > self.settings.stall_timeout:
            raise TemporaryError("FFmpeg media progress stalled")

    async def stop(self) -> None:
        if self.process is not None:
            await terminate(self.process, self.settings.shutdown_timeout)
            for reader in self.readers:
                reader.cancel()
            await asyncio.gather(*self.readers, return_exceptions=True)
            event(logger, "ffmpeg_stopped", exit_code=self.process.returncode)
            self.process = None
            self.readers = []

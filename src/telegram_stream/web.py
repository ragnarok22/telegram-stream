"""Credential-free Chromium/Xvfb sidecar exposing a private, silent HLS feed."""

import asyncio
import json
import logging
import os
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket
from socketserver import BaseServer
from typing import Self
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from websockets.asyncio.client import connect

from telegram_stream.config import _bitrate, _number
from telegram_stream.errors import FatalError, TemporaryError
from telegram_stream.logging import configure, event, redactor
from telegram_stream.process import spawn, terminate

logger = logging.getLogger(__name__)
STREAM_DIRECTORY = Path("/tmp/web-stream")


@dataclass(frozen=True)
class WebSettings:
    url: str = field(repr=False)
    width: int = 720
    height: int = 1280
    fps: int = 30
    timeout: int = 60
    queue_size: int = 16
    video_bitrate: int = 2_500_000
    capture_fps: int | None = None
    capture_scale: int = 2

    @classmethod
    def from_env(cls) -> Self:
        url = os.environ.get("PAGE_URL", "")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise FatalError("PAGE_URL must be an HTTP or HTTPS page")
        fps = _number(os.environ, "WEB_FPS", 30, 1, 60)
        settings = cls(
            url=url,
            width=_number(os.environ, "WEB_WIDTH", 720, 128, 4096),
            height=_number(os.environ, "WEB_HEIGHT", 1280, 128, 4096),
            fps=fps,
            timeout=_number(os.environ, "WEB_READY_TIMEOUT", 60, 10, 300),
            queue_size=_number(os.environ, "WEB_QUEUE_SIZE", 16, 1, 64),
            video_bitrate=_bitrate(
                os.environ.get("WEB_VIDEO_BITRATE", "2500k"), "WEB_VIDEO_BITRATE"
            ),
            capture_fps=_number(os.environ, "WEB_CAPTURE_FPS", fps, 1, 60),
            capture_scale=_number(os.environ, "WEB_CAPTURE_SCALE", 2, 1, 2),
        )
        if settings.effective_capture_fps > settings.fps:
            raise FatalError("WEB_CAPTURE_FPS must not exceed WEB_FPS")
        if settings.width % 2 or settings.height % 2 or settings.capture_height > 8192:
            raise FatalError("Web capture requires even dimensions and a reasonable aspect ratio")
        return settings

    @property
    def effective_capture_fps(self) -> int:
        return self.fps if self.capture_fps is None else self.capture_fps

    @property
    def capture_width(self) -> int:
        # Keep Chrome above its minimum CSS window width, including at native 1x.
        # The default 2x preserves the existing supersampled capture geometry.
        return max(540 * self.capture_scale, self.width)

    @property
    def capture_height(self) -> int:
        return round(self.capture_width * self.height / self.width / 2) * 2


def chromium_command(settings: WebSettings) -> list[str]:
    return [
        "chromium",
        "--kiosk",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--noerrdialogs",
        "--disable-gpu",
        "--hide-scrollbars",
        "--disable-session-crashed-bubble",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=Translate,TranslateUI",
        f"--force-device-scale-factor={settings.capture_scale}",
        f"--window-size={settings.capture_width // settings.capture_scale},"
        f"{settings.capture_height // settings.capture_scale}",
        "--window-position=0,0",
        "--mute-audio",
        "--user-data-dir=/tmp/chromium-profile",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9222",
        settings.url,
    ]


def capture_command(settings: WebSettings, directory: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-thread_queue_size",
        str(settings.queue_size),
        "-f",
        "x11grab",
        "-draw_mouse",
        "0",
        "-video_size",
        f"{settings.capture_width}x{settings.capture_height}",
        "-framerate",
        str(settings.effective_capture_fps),
        "-i",
        ":99.0",
        "-vf",
        (
            f"scale={settings.width}:{settings.height},setsar=1"
            + (f",fps={settings.fps}" if settings.effective_capture_fps < settings.fps else "")
        ),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-profile:v",
        "main",
        "-b:v",
        str(settings.video_bitrate),
        "-maxrate",
        str(settings.video_bitrate),
        "-bufsize",
        str(settings.video_bitrate * 2),
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(settings.fps),
        "-keyint_min",
        str(settings.fps),
        "-sc_threshold",
        "0",
        "-an",
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "6",
        "-hls_start_number_source",
        "epoch_us",
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename",
        str(directory / "segment%019d.ts"),
        str(directory / "index.m3u8"),
    ]


def _tabs() -> list[dict[str, object]]:
    # DevTools stays on loopback. Never use an operator's proxy for this request.
    with build_opener(ProxyHandler({})).open(
        "http://127.0.0.1:9222/json/list", timeout=3
    ) as response:
        result = json.load(response)
    if not isinstance(result, list):
        raise TemporaryError("Invalid browser DevTools response")
    return result


async def page_ready() -> bool:
    tabs = await asyncio.to_thread(_tabs)
    for tab in tabs:
        url = tab.get("url")
        if tab.get("type") != "page" or not isinstance(url, str):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        websocket_url = tab.get("webSocketDebuggerUrl")
        if not isinstance(websocket_url, str):
            continue
        async with asyncio.timeout(8):
            async with connect(
                websocket_url, proxy=None, open_timeout=3, close_timeout=1, max_size=65536
            ) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": (
                                    "document.readyState === 'complete' && !!document.body && "
                                    "!document.documentURI.startsWith('chrome-error:') && "
                                    "document.documentElement.getBoundingClientRect().width > 0"
                                ),
                                "returnByValue": True,
                            },
                        }
                    )
                )
                while True:
                    reply = json.loads(await socket.recv())
                    if reply.get("id") == 1:
                        return reply.get("result", {}).get("result", {}).get("value") is True
    return False


class WebCapture:
    def __init__(self, settings: WebSettings) -> None:
        self.settings = settings
        self.processes: list[asyncio.subprocess.Process] = []
        self.browser_checked_at = 0.0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def playlist_fresh(self) -> bool:
        playlist = STREAM_DIRECTORY / "index.m3u8"
        try:
            return 0 <= time.time() - playlist.stat().st_mtime < 15
        except OSError:
            return False

    @property
    def healthy(self) -> bool:
        return (
            self.playlist_fresh
            and time.monotonic() - self.browser_checked_at < 20
            and len(self.processes) == 3
            and all(process.returncode is None for process in self.processes)
        )

    def stream_info(self) -> dict[str, object]:
        """Describe only the controlled encoder contract, never page configuration."""
        return {
            "version": 1,
            "ready": self.healthy,
            "codec": "h264",
            "profile": "main",
            "pix_fmt": "yuv420p",
            "width": self.settings.width,
            "height": self.settings.height,
            "fps": self.settings.fps,
            "sample_aspect_ratio": "1:1",
            "video_bitrate": self.settings.video_bitrate,
            "maxrate": self.settings.video_bitrate,
            "gop": self.settings.fps,
        }

    async def _spawn(self, command: list[str]) -> None:
        process = await spawn(
            command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        self.processes.append(process)

    async def run(self) -> None:
        # Chromium's crash reporter also needs a writable HOME/config location.
        # Both are private tmpfs paths in the sidecar, never host/browser profiles.
        Path.home().mkdir(parents=True, mode=0o700, exist_ok=True)
        STREAM_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
        self.server = ThreadingHTTPServer(
            ("0.0.0.0", 8080),
            partial(StreamHandler, directory=str(STREAM_DIRECTORY), capture=self),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        try:
            os.environ["DISPLAY"] = ":99"
            await self._spawn(
                [
                    "Xvfb",
                    ":99",
                    "-screen",
                    "0",
                    f"{self.settings.capture_width}x{self.settings.capture_height}x24",
                    "-ac",
                    "-nolisten",
                    "tcp",
                ]
            )
            async with asyncio.timeout(10):
                while not Path("/tmp/.X11-unix/X99").exists():
                    self._check_processes()
                    await asyncio.sleep(0.1)
            await self._spawn(chromium_command(self.settings))
            deadline = time.monotonic() + self.settings.timeout
            while True:
                self._check_processes()
                try:
                    if await page_ready():
                        break
                except (OSError, TimeoutError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise TemporaryError("Web page did not become ready before its deadline")
                await asyncio.sleep(1)
            # Allow fonts, images, and initial client-side requests to paint.
            await asyncio.sleep(2)
            self.browser_checked_at = time.monotonic()
            await self._spawn(capture_command(self.settings, STREAM_DIRECTORY))
            encoder_started = time.monotonic()
            event(
                logger,
                "web_capture_started",
                width=self.settings.width,
                height=self.settings.height,
            )
            while True:
                self._check_processes()
                if not await page_ready():
                    raise TemporaryError("Browser page stopped responding")
                self.browser_checked_at = time.monotonic()
                playlist = STREAM_DIRECTORY / "index.m3u8"
                if not self.playlist_fresh and (
                    playlist.exists() or time.monotonic() - encoder_started > self.settings.timeout
                ):
                    raise TemporaryError("Web encoder stopped producing HLS segments")
                await asyncio.sleep(5)
        finally:
            self.browser_checked_at = 0
            for process in reversed(self.processes):
                await terminate(process)
            if self.server is not None:
                await asyncio.to_thread(self.server.shutdown)
                self.server.server_close()
            if self.thread is not None:
                await asyncio.to_thread(self.thread.join)

    def _check_processes(self) -> None:
        for index, process in enumerate(self.processes):
            if process.returncode is not None:
                name = ("display", "browser", "encoder")[index]
                raise TemporaryError(f"Web {name} exited (code {process.returncode})")


class StreamHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        request: socket,
        client_address: tuple[str, int],
        server: BaseServer,
        *,
        directory: str,
        capture: WebCapture,
    ) -> None:
        self.capture = capture
        super().__init__(request, client_address, server, directory=directory)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_response(200 if self.capture.healthy else 503)
            self.end_headers()
            return
        if path == "/stream-info":
            info = self.capture.stream_info()
            body = json.dumps(info).encode()
            self.send_response(200 if info["ready"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path != "/index.m3u8" and not re.fullmatch(r"/segment\d+\.ts", path):
            self.send_error(404)
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Polling and segment requests are intentionally not logged.
        pass


async def serve(settings: WebSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    runner = asyncio.create_task(WebCapture(settings).run())
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if runner in done:
            await runner
    finally:
        runner.cancel()
        stopper.cancel()
        await asyncio.gather(runner, stopper, return_exceptions=True)


def main() -> None:
    os.umask(0o077)
    configure()
    try:
        settings = WebSettings.from_env()
        redactor.add(settings.url)
        asyncio.run(serve(settings))
    except (FatalError, TemporaryError) as error:
        event(logger, "web_capture_failed", reason=str(error))
        raise SystemExit(1) from None
    except Exception as error:
        event(logger, "web_capture_failed", error=type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

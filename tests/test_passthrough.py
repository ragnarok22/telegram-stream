import asyncio
import json
import logging
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from telegram_stream.config import Settings
from telegram_stream.ffmpeg import FFmpeg, build_command
from telegram_stream.process import capture, spawn
from telegram_stream.sources import (
    MediaInput,
    ResolvedSource,
    SourceResolver,
    _fetch_web_copy_contract,
    _read_web_copy_contract,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        123, "a" * 32, source_type="web", source="https://page.invalid", web_video_copy=True
    )


@pytest.fixture
def video() -> dict[str, object]:
    return {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1280,
        "height": 720,
        "pix_fmt": "yuv420p",
        "profile": "Constrained Baseline",
        "level": 31,
        "field_order": "progressive",
        "sample_aspect_ratio": "1:1",
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
    }


@pytest.fixture
def contract() -> dict[str, object]:
    return {
        "version": 1,
        "ready": True,
        "codec": "h264",
        "profile": "main",
        "pix_fmt": "yuv420p",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "sample_aspect_ratio": "1:1",
        "video_bitrate": 2_500_000,
        "maxrate": 2_500_000,
        "gop": 30,
    }


@pytest.fixture
def contract_calls(
    monkeypatch: pytest.MonkeyPatch, video: dict[str, object], contract: dict[str, object]
) -> list[str]:
    calls: list[str] = []

    async def fake_capture(command: list[str], timeout: float) -> bytes:
        assert command[0] == "ffprobe"
        return json.dumps({"streams": [video]}).encode()

    async def fake_contract(location: str, timeout: int) -> object:
        calls.append(location)
        return contract

    monkeypatch.setattr("telegram_stream.sources.capture", fake_capture)
    monkeypatch.setattr("telegram_stream.sources._fetch_web_copy_contract", fake_contract)
    return calls


@pytest.mark.parametrize("audio_index", [None, 0])
def test_compatible_web_copies_only_video_and_keeps_aac_and_progress(
    settings: Settings, contract_calls: list[str], audio_index: int | None
) -> None:
    source = asyncio.run(SourceResolver(settings).resolve())
    assert source.video_copy
    assert contract_calls == [settings.web_stream_url]
    command = build_command(settings, replace(source, audio_index=audio_index), "output.flv")
    assert command[command.index("-c:v") + 1] == "copy"
    for option in (
        "-vf",
        "-preset",
        "-pix_fmt",
        "-profile:v",
        "-b:v",
        "-maxrate",
        "-bufsize",
        "-g",
        "-keyint_min",
        "-sc_threshold",
    ):
        assert option not in command
    assert "libx264" not in command
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-progress") + 1] == "pipe:1"
    assert "aresample=async=1:first_pts=0" in command
    assert "-shortest" in command
    assert command[command.index("-map") + 1] == "0:v:0"
    silence = "anullsrc=channel_layout=stereo:sample_rate=44100"
    assert (silence in command) == (audio_index is None)
    assert f"{1 if audio_index is None else 0}:a:0" in command


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("codec_name", "hevc"),
        ("width", 1920),
        ("height", 1080),
        ("pix_fmt", "yuv420p10le"),
        ("pix_fmt", "yuv444p"),
        ("sample_aspect_ratio", "4:3"),
        ("sample_aspect_ratio", "0:1"),
        ("sample_aspect_ratio", "1:0"),
        ("r_frame_rate", "30000/1001"),
        ("avg_frame_rate", "29/1"),
        ("avg_frame_rate", "0/0"),
        ("avg_frame_rate", "NaN"),
        ("profile", "High 4:4:4 Predictive"),
        ("profile", []),
        ("level", 9),
        ("level", 30),  # Level 3.0 cannot carry 720p30.
        ("level", 51),
        ("field_order", "tt"),
        ("field_order", "unknown"),
        ("bit_rate", "2500001"),
        ("bit_rate", "invalid"),
        ("bit_rate", False),
        ("max_bit_rate", "3000000"),
        ("tags", {"rotate": "90"}),
        ("side_data_list", [{"rotation": -90}]),
    ],
)
def test_incompatible_metadata_transcodes_without_reading_contract(
    settings: Settings,
    video: dict[str, object],
    contract_calls: list[str],
    field: str,
    value: object,
) -> None:
    video[field] = value
    source = asyncio.run(SourceResolver(settings).resolve())
    assert not source.video_copy
    assert not contract_calls
    command = build_command(settings, source, "output.flv")
    assert command[command.index("-c:v") + 1] == "libx264"
    assert "-vf" in command
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in command


@pytest.mark.parametrize(
    "field",
    [
        "codec_name",
        "width",
        "height",
        "pix_fmt",
        "profile",
        "level",
        "sample_aspect_ratio",
        "r_frame_rate",
        "avg_frame_rate",
    ],
)
def test_unknown_metadata_transcodes(
    settings: Settings,
    video: dict[str, object],
    contract_calls: list[str],
    field: str,
) -> None:
    del video[field]
    assert not asyncio.run(SourceResolver(settings).resolve()).video_copy
    assert not contract_calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("version", True),
        ("ready", False),
        ("codec", "hevc"),
        ("profile", "high"),
        ("pix_fmt", "yuv422p"),
        ("width", 720),
        ("height", 1280),
        ("fps", 29),
        ("fps", "30"),
        ("sample_aspect_ratio", "4:3"),
        ("video_bitrate", 3_000_000),
        ("video_bitrate", True),
        ("maxrate", 2_500_001),
        ("maxrate", 0),
        ("maxrate", "2500000"),
        ("gop", 61),
        ("gop", -1),
        ("gop", True),
    ],
)
def test_incompatible_contract_transcodes(
    settings: Settings,
    contract: dict[str, object],
    contract_calls: list[str],
    field: str,
    value: object,
) -> None:
    contract[field] = value
    assert not asyncio.run(SourceResolver(settings).resolve()).video_copy
    assert contract_calls


def test_missing_contract_fields_transcode(
    settings: Settings, contract: dict[str, object], contract_calls: list[str]
) -> None:
    contract.clear()
    assert not asyncio.run(SourceResolver(settings).resolve()).video_copy
    assert contract_calls


@pytest.mark.parametrize("field", ["bit_rate", "max_bit_rate"])
def test_probe_cannot_contradict_sidecar_bitrate_bound(
    settings: Settings,
    video: dict[str, object],
    contract: dict[str, object],
    contract_calls: list[str],
    field: str,
) -> None:
    video[field] = "2000000"
    contract.update(video_bitrate=1_000_000, maxrate=1_000_000)
    assert not asyncio.run(SourceResolver(settings).resolve()).video_copy
    assert contract_calls


def test_unknown_field_order_only_allowed_for_non_interlaced_baseline(
    settings: Settings, video: dict[str, object], contract_calls: list[str]
) -> None:
    del video["field_order"]
    assert asyncio.run(SourceResolver(settings).resolve()).video_copy
    video["profile"] = "Main"
    assert not asyncio.run(SourceResolver(settings).resolve()).video_copy
    assert len(contract_calls) == 1


@pytest.mark.parametrize("kind", ["web", "youtube", "hls", "file"])
def test_copy_is_opt_in_and_never_changes_other_source_kinds(settings: Settings, kind: str) -> None:
    source = ResolvedSource((MediaInput("http://example.invalid/index.m3u8"),), 0, None)
    assert not source.video_copy
    for enabled in (False, True):
        configured = replace(settings, source_type=kind, web_video_copy=enabled)
        command = build_command(configured, replace(source, video_copy=True), "output.flv")
        codec = command[command.index("-c:v") + 1]
        assert codec == ("copy" if kind == "web" and enabled else "libx264")
    local = build_command(settings, replace(source, video_copy=True, local=True), "output.flv")
    assert local[local.index("-c:v") + 1] == "libx264"


def test_disabled_copy_does_not_request_contract_or_optional_probe_metadata(
    settings: Settings,
    contract_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_capture(command: list[str], timeout: float) -> bytes:
        assert command[command.index("-show_entries") + 1] == "stream=codec_type"
        return b'{"streams":[{"codec_type":"video"}]}'

    monkeypatch.setattr("telegram_stream.sources.capture", fake_capture)
    assert not asyncio.run(
        SourceResolver(replace(settings, web_video_copy=False)).resolve()
    ).video_copy
    assert not contract_calls


def test_unavailable_contract_falls_back_before_startup(
    settings: Settings,
    contract_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(location: str, timeout: int) -> object:
        raise TimeoutError

    monkeypatch.setattr("telegram_stream.sources._fetch_web_copy_contract", unavailable)
    source = asyncio.run(SourceResolver(settings).resolve())
    command = build_command(settings, source, "output.flv")
    assert command[command.index("-c:v") + 1] == "libx264"


@pytest.mark.parametrize("copy", [False, True])
def test_started_event_reports_effective_video_mode_without_urls(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    copy: bool,
) -> None:
    async def fake_spawn(command: list[str]) -> asyncio.subprocess.Process:
        assert command[command.index("-c:v") + 1] == ("copy" if copy else "libx264")
        return await spawn([sys.executable, "-c", "pass"])

    async def scenario() -> None:
        source = ResolvedSource(
            (MediaInput("http://example.invalid/private-token"),), 0, None, video_copy=copy
        )
        publisher = FFmpeg(settings)
        try:
            await publisher.start(source, "rtmp://example.invalid/private-key")
            assert publisher.process is not None
            await asyncio.wait_for(publisher.process.wait(), 5)
        finally:
            await publisher.stop()

    monkeypatch.setattr("telegram_stream.ffmpeg.spawn", fake_spawn)
    caplog.set_level(logging.INFO, logger="telegram_stream.ffmpeg")
    asyncio.run(scenario())
    records = [record for record in caplog.records if record.getMessage() == "ffmpeg_started"]
    assert len(records) == 1
    fields = getattr(records[0], "fields", {})
    assert fields["video_mode"] == ("copy" if copy else "transcode")
    assert set(fields) == {"pid", "video_mode"}


@contextmanager
def sidecar(
    directory: Path, contract: dict[str, object], *, status: int = 200, payload: bytes | None = None
) -> Generator[str]:
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/stream-info":
                self.send_response(status)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Location", "/index.m3u8")
                self.end_headers()
                self.wfile.write(payload if payload is not None else json.dumps(contract).encode())
            else:
                super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.m3u8"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (404, None),
        (503, None),
        (302, None),
        (200, b"not json"),
        (200, b" " * 4097),
        (200, b"[" * 1500 + b"]" * 1500),
    ],
)
def test_invalid_http_contract_is_ignored(
    tmp_path: Path, contract: dict[str, object], status: int, payload: bytes | None
) -> None:
    with sidecar(tmp_path, contract, status=status, payload=payload) as location:
        assert _read_web_copy_contract(location, 2) is None


def test_contract_request_does_not_use_operator_proxy(
    tmp_path: Path, contract: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    with sidecar(tmp_path, contract) as location:
        assert _read_web_copy_contract(location, 2) == contract


def test_supervised_contract_reader_returns_metadata(
    tmp_path: Path, contract: dict[str, object]
) -> None:
    with sidecar(tmp_path, contract) as location:
        assert asyncio.run(_fetch_web_copy_contract(location, 2)) == contract


def test_contract_timeout_closes_slow_http_reader(
    settings: Settings,
    video: dict[str, object],
    contract: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disconnected = threading.Event()
    stop = threading.Event()

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(contract).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    if stop.wait(0.05):
                        return
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
            except OSError:
                disconnected.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    async def fake_capture(command: list[str], timeout: float) -> bytes:
        if command[0] == "ffprobe":
            return json.dumps({"streams": [video]}).encode()
        return await capture(command, timeout)

    monkeypatch.setattr("telegram_stream.sources.capture", fake_capture)
    with ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()

        async def scenario() -> None:
            try:
                source = await SourceResolver(
                    replace(
                        settings,
                        rpc_timeout=2,
                        web_stream_url=f"http://127.0.0.1:{server.server_port}/index.m3u8",
                    )
                ).resolve()
                assert not source.video_copy
                assert disconnected.wait(1), "HTTP reader survived the resolution timeout"
            finally:
                stop.set()

        try:
            asyncio.run(scenario())
        finally:
            stop.set()
            server.shutdown()
            thread.join(timeout=2)

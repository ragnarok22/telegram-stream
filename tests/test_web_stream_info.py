"""The private compatibility endpoint exposes encoder facts, never page secrets."""

import json
import threading
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from telegram_stream.web import StreamHandler, WebCapture, WebSettings


@pytest.mark.parametrize("healthy", [False, True])
def test_stream_info_matches_encoder_and_reports_health_without_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, healthy: bool
) -> None:
    settings = WebSettings(
        "https://example.invalid/?secret=private",
        width=720,
        height=1280,
        fps=30,
        video_bitrate=1_500_000,
    )
    capture = WebCapture(settings)
    monkeypatch.setattr(WebCapture, "healthy", property(lambda self: healthy))
    handler = partial(StreamHandler, directory=str(tmp_path), capture=capture)
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/stream-info")
            response = connection.getresponse()
            assert response.status == (200 if healthy else 503)
            assert response.getheader("Content-Type") == "application/json"
            assert response.getheader("Cache-Control") == "no-store"
            body = response.read()
            assert b"private" not in body
            assert b"example.invalid" not in body
            assert json.loads(body) == {
                "version": 1,
                "ready": healthy,
                "codec": "h264",
                "profile": "main",
                "pix_fmt": "yuv420p",
                "width": 720,
                "height": 1280,
                "fps": 30,
                "sample_aspect_ratio": "1:1",
                "video_bitrate": 1_500_000,
                "maxrate": 1_500_000,
                "gop": 30,
            }
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=2)

"""Bound the raw X11 input queue without starting any capture processes."""

import os
from pathlib import Path

import pytest

from telegram_stream.errors import FatalError
from telegram_stream.web import WebSettings, capture_command


@pytest.fixture(autouse=True)
def web_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAGE_URL", "https://example.invalid")
    for name in tuple(os.environ):
        if name.startswith("WEB_"):
            monkeypatch.delenv(name)


def test_capture_queue_defaults_to_16_frames(tmp_path: Path) -> None:
    assert WebSettings("https://example.invalid").queue_size == 16
    settings = WebSettings.from_env()
    assert settings.queue_size == 16

    command = capture_command(settings, tmp_path)
    assert command.count("-thread_queue_size") == 1
    queue_index = command.index("-thread_queue_size")
    assert command[queue_index + 1] == "16"
    assert queue_index < command.index("-i")


@pytest.mark.parametrize("queue_size", [1, 32, 64])
def test_capture_queue_uses_validated_environment_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, queue_size: int
) -> None:
    monkeypatch.setenv("WEB_QUEUE_SIZE", str(queue_size))
    settings = WebSettings.from_env()
    assert settings.queue_size == queue_size

    command = capture_command(settings, tmp_path)
    assert command[command.index("-thread_queue_size") + 1] == str(queue_size)


@pytest.mark.parametrize("queue_size", ["-1", "0", "65", "512"])
def test_capture_queue_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch, queue_size: str
) -> None:
    monkeypatch.setenv("WEB_QUEUE_SIZE", queue_size)
    with pytest.raises(FatalError, match="WEB_QUEUE_SIZE must be between 1 and 64"):
        WebSettings.from_env()


@pytest.mark.parametrize("queue_size", ["", "1.5", "sixteen"])
def test_capture_queue_rejects_non_integer_values(
    monkeypatch: pytest.MonkeyPatch, queue_size: str
) -> None:
    monkeypatch.setenv("WEB_QUEUE_SIZE", queue_size)
    with pytest.raises(FatalError, match="WEB_QUEUE_SIZE must be an integer"):
        WebSettings.from_env()

import json
import logging
import time
from pathlib import Path

import pytest

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError
from telegram_stream.health import read_status
from telegram_stream.logging import JsonFormatter, redactor
from telegram_stream.retry import Backoff
from telegram_stream.state import InstanceLock, StateStore, StoryState, atomic_json


def test_lock_prevents_two_session_writers(tmp_path: Path) -> None:
    with (
        InstanceLock(tmp_path),
        pytest.raises(FatalError, match="Another"),
        InstanceLock(tmp_path),
    ):
        pytest.fail("A second writer acquired the session")


def test_invalid_state_never_silently_creates_a_new_story(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.path.write_text("{truncated")
    with pytest.raises(FatalError, match="Invalid"):
        store.load(42)
    store.save(StoryState(42, 123))
    with pytest.raises(FatalError, match="another channel"):
        store.load(43)


def test_health_is_independent_of_telegram_and_checks_staleness(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    atomic_json(path, {"phase": "recovering", "last_tick": time.time(), "watchdog_timeout": 180})
    assert read_status(tmp_path)["healthy"]
    atomic_json(path, {"phase": "running", "last_tick": time.time() - 181, "watchdog_timeout": 180})
    assert not read_status(tmp_path)["healthy"]
    atomic_json(path, {"phase": "stopped", "last_tick": time.time(), "watchdog_timeout": 180})
    assert not read_status(tmp_path)["healthy"]


@pytest.mark.parametrize(("phase", "age"), [("running", 181), ("stopped", 0), ("fatal", 0)])
def test_unhealthy_status_cannot_remain_ready(tmp_path: Path, phase: str, age: int) -> None:
    atomic_json(
        tmp_path / "health.json",
        {
            "phase": phase,
            "ready": True,
            "last_tick": time.time() - age,
            "watchdog_timeout": 180,
        },
    )
    status = read_status(tmp_path)
    assert status["healthy"] is False
    assert status["ready"] is False


def test_logs_are_valid_json_and_redact_nested_secrets() -> None:
    redactor.add("test-secret-key")
    record = logging.LogRecord("test", logging.INFO, "", 0, "source_retry", (), None)
    record.fields = {
        "detail": "Failed to publish rtmp://host/live/test-secret-key",
        "nested": ["https://example.invalid/?token=private", "test-secret-key"],
    }
    encoded = JsonFormatter().format(record)
    assert json.loads(encoded)["event"] == "source_retry"
    assert "test-secret-key" not in encoded
    assert "private" not in encoded


def test_configuration_and_backoff_validation() -> None:
    env = {
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "a" * 32,
        "TELEGRAM_CHANNEL": "@test",
        "SOURCE": "https://example.invalid/live.m3u8",
        "VIDEO_WIDTH": "1279",
    }
    with pytest.raises(FatalError, match="even"):
        Settings.from_env(env)
    env["VIDEO_WIDTH"] = "1280"
    assert Settings.from_env(env).kind == "hls"
    backoff = Backoff(2, 8)
    for low, high in [(1, 2), (2, 4), (4, 8), (4, 8)]:
        assert low <= backoff.next_delay() <= high
    backoff.reset()
    assert 1 <= backoff.next_delay() <= 2

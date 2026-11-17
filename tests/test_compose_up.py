"""Compose startup decisions without contacting a Docker daemon."""

import json
import runpy
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compose_up.py"
main = cast(Callable[[list[str]], int], runpy.run_path(str(SCRIPT))["main"])
SECRET = "private-telegram-credential"


class ComposeStub:
    def __init__(self) -> None:
        self.environment: dict[str, object] = {"SOURCE_TYPE": "auto", "TELEGRAM_API_HASH": SECRET}
        self.calls: list[list[str]] = []
        self.fail_at: int | None = None
        self.output: str | None = None

    def run(
        self, command: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output and text and not check
        self.calls.append(command)
        configuration = {"services": {"streamer": {"environment": self.environment}}}
        output = self.output if self.output is not None else json.dumps(configuration)
        return subprocess.CompletedProcess(
            command,
            17 if len(self.calls) == self.fail_at else 0,
            output if "config" in command else SECRET,
            SECRET,
        )


@pytest.fixture
def compose(monkeypatch: pytest.MonkeyPatch) -> ComposeStub:
    stub = ComposeStub()
    monkeypatch.setattr(subprocess, "run", stub.run)
    return stub


@pytest.mark.parametrize("source_type", ["web", "WEB"])
def test_web_is_healthy_before_starting_streamer(compose: ComposeStub, source_type: str) -> None:
    compose.environment["SOURCE_TYPE"] = source_type
    assert main([]) == 0
    assert compose.calls == [
        ["docker", "compose", "--profile", "web", "config", "--format", "json"],
        [
            "docker",
            "compose",
            "--profile",
            "web",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "120",
            "web",
        ],
        ["docker", "compose", "--profile", "web", "up", "-d", "--no-deps", "streamer"],
    ]


@pytest.mark.parametrize("source_type", ["youtube", "hls", "file", "auto"])
def test_non_web_stops_old_web_after_streamer_starts(
    compose: ComposeStub, monkeypatch: pytest.MonkeyPatch, source_type: str
) -> None:
    # The effective container setting wins over the helper's host environment.
    monkeypatch.setenv("SOURCE_TYPE", "web")
    compose.environment["SOURCE_TYPE"] = source_type
    assert main([]) == 0
    assert len(compose.calls) == 3
    assert compose.calls[1] == [
        "docker",
        "compose",
        "--profile",
        "web",
        "up",
        "-d",
        "--no-deps",
        "streamer",
    ]
    assert compose.calls[2] == ["docker", "compose", "--profile", "web", "stop", "web"]


@pytest.mark.parametrize("missing", [False, True])
def test_unset_source_defaults_to_auto(compose: ComposeStub, missing: bool) -> None:
    if missing:
        compose.environment.pop("SOURCE_TYPE")
    else:
        compose.environment["SOURCE_TYPE"] = None
    assert main([]) == 0
    assert compose.calls[-1][-2:] == ["stop", "web"]


def test_build_and_compose_options_apply_to_same_project(compose: ComposeStub) -> None:
    compose.environment["SOURCE_TYPE"] = "web"
    assert (
        main(
            [
                "--build",
                "-f",
                "docker-compose.yml",
                "-f",
                "override.yml",
                "--env-file",
                ".env",
                "--env-file",
                "override.env",
                "-p",
                "other-project",
                "--project-directory",
                "/project",
            ]
        )
        == 0
    )
    prefix = [
        "docker",
        "compose",
        "--file",
        "docker-compose.yml",
        "--file",
        "override.yml",
        "--env-file",
        ".env",
        "--env-file",
        "override.env",
        "--project-name",
        "other-project",
        "--project-directory",
        "/project",
        "--profile",
        "web",
    ]
    assert compose.calls == [
        [*prefix, "config", "--format", "json"],
        [*prefix, "up", "-d", "--no-deps", "--build", "--wait", "--wait-timeout", "120", "web"],
        [*prefix, "up", "-d", "--no-deps", "--build", "streamer"],
    ]


def test_web_readiness_failure_preserves_streamer(
    compose: ComposeStub, capsys: pytest.CaptureFixture[str]
) -> None:
    compose.environment["SOURCE_TYPE"] = "web"
    compose.fail_at = 2
    assert main([]) == 1
    assert len(compose.calls) == 2
    assert compose.calls[-1][-4:] == ["--wait", "--wait-timeout", "120", "web"]
    assert all(command[-1] != "streamer" and "stop" not in command for command in compose.calls)
    captured = capsys.readouterr()
    assert "could not start web" in captured.err
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize(
    ("source_type", "fail_at"),
    [("auto", 1), ("auto", 2), ("web", 3)],
)
def test_start_failure_never_stops_web(
    compose: ComposeStub, capsys: pytest.CaptureFixture[str], source_type: str, fail_at: int
) -> None:
    compose.environment["SOURCE_TYPE"] = source_type
    compose.fail_at = fail_at
    assert main([]) == 1
    assert len(compose.calls) == fail_at
    assert all("stop" not in command for command in compose.calls)
    captured = capsys.readouterr()
    assert "exit code 17" in captured.err
    assert SECRET not in captured.out + captured.err


def test_stop_failure_is_reported(compose: ComposeStub, capsys: pytest.CaptureFixture[str]) -> None:
    compose.fail_at = 3
    assert main([]) == 1
    assert compose.calls[-1][-2:] == ["stop", "web"]
    assert "could not stop web" in capsys.readouterr().err


@pytest.mark.parametrize("source_type", ["", SECRET, 123, ["web"]])
def test_invalid_source_type_fails_before_starting(
    compose: ComposeStub, capsys: pytest.CaptureFixture[str], source_type: object
) -> None:
    compose.environment["SOURCE_TYPE"] = source_type
    assert main([]) == 1
    assert len(compose.calls) == 1
    captured = capsys.readouterr()
    assert "Effective SOURCE_TYPE" in captured.err
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize(
    "output",
    [
        SECRET,
        "[]",
        "{}",
        '{"services": {"streamer": null}}',
        '{"services": {"streamer": {"environment": []}}}',
    ],
)
def test_invalid_configuration_is_not_printed(
    compose: ComposeStub, capsys: pytest.CaptureFixture[str], output: str
) -> None:
    compose.output = output
    assert main([]) == 1
    assert len(compose.calls) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_success_does_not_log_configuration_or_subprocess_output(
    compose: ComposeStub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "Stopping unused web" in captured.out
    assert SECRET not in captured.out + captured.err


def test_missing_docker_is_a_safe_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(
        command: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(SECRET)

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert main([]) == 1
    captured = capsys.readouterr()
    assert "could not read effective configuration" in captured.err
    assert SECRET not in captured.out + captured.err

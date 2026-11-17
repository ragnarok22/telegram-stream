"""Exact counter deltas and a read-only, project-scoped Docker interface."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def stats() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "resource_stats.py"
    spec = importlib.util.spec_from_file_location("resource_stats", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exact_deltas_use_real_elapsed_time_not_requested_interval(stats: ModuleType) -> None:
    before = stats.Sample(
        "a" * 64,
        "streamer",
        "start",
        "running",
        "healthy",
        {"clock": 10_000_000_000, "cpu": 80_000_000_000, "ram": 200, "read": 9000, "write": 500},
        {"eth0": [1_000_000_001, 2_000_000_003], "eth1": [11, 23]},
    )
    after = stats.Sample(
        "a" * 64,
        "streamer",
        "start",
        "running",
        "healthy",
        {"clock": 12_500_000_000, "cpu": 83_750_000_000, "ram": 300, "read": 9501, "write": 999},
        {"eth0": [1_000_000_008, 2_000_000_022], "eth1": [16, 30]},
    )
    change = stats.delta(before, after)
    assert change == {
        "clock": 2_500_000_000,
        "cpu": 3_750_000_000,
        "read": 501,
        "write": 499,
        "rx": 12,
        "tx": 26,
    }
    assert change["cpu"] / change["clock"] * 100 == 150
    assert change["rx"] / (change["clock"] / 1e9) == 4.8
    summaries = {}
    stats.report([after], {before.container: before}, summaries)
    total = summaries[("streamer", "a" * 64)]
    assert total.seconds == 2.5
    assert total.rx == 12
    assert total.peak_cpu == 150
    assert total.peak_ram == 300


@pytest.mark.parametrize("reset", ["container", "started", "cpu", "clock", "network", "interface"])
def test_restarts_and_each_interface_counter_reset_discard_interval(
    stats: ModuleType,
    reset: str,
) -> None:
    before = stats.Sample(
        "a" * 64,
        "web",
        "start",
        "running",
        "healthy",
        {"clock": 1, "cpu": 100, "ram": 1, "read": 20, "write": 30},
        {"eth0": [100, 200], "eth1": [100, 200]},
    )
    after = stats.Sample(
        "a" * 64,
        "web",
        "start",
        "running",
        "healthy",
        {"clock": 2, "cpu": 110, "ram": 1, "read": 30, "write": 40},
        {"eth0": [110, 210], "eth1": [110, 210]},
    )
    if reset in {"container", "started"}:
        setattr(after, reset, "changed")
    elif reset in {"cpu", "clock"}:
        after.counters[reset] = 0
    elif reset == "network":
        after.network["eth0"][0] = 99
        after.network["eth1"][0] = 999  # An aggregate alone would conceal this reset.
    else:
        after.network["eth2"] = [1, 1]
    assert stats.delta(before, after) is None
    assert stats.delta(None, after) is None


def test_compose_scope_stopped_and_disappearing_containers(
    stats: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = ["a" * 64, "b" * 64, "c" * 64]
    commands = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["cwd"] == stats.ROOT
        assert kwargs["timeout"] == 15
        assert kwargs["stderr"] == subprocess.DEVNULL
        if command[1] == "compose":
            assert command == ["docker", "compose", "--profile", "web", "ps", "--all", "--quiet"]
            return subprocess.CompletedProcess(command, 0, "\n".join(ids), "")
        if command[1] == "inspect":
            assert ".Config.Env" not in command[3] and ".State.Health.Log" not in command[3]
            if command[-1] == ids[2]:
                return subprocess.CompletedProcess(command, 1, "", "password=do-not-print")
            metadata = {
                "service": "streamer" if command[-1] == ids[0] else "web",
                "started": "start",
                "state": "running" if command[-1] == ids[0] else "exited",
                "health": "healthy",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")
        assert command[:5] == ["docker", "exec", ids[0], "python3", "-B"]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "counters": {"clock": 1, "cpu": 2, "ram": 3, "read": 4, "write": 5},
                    "network": {"eth0": [7, 8]},
                    "healthy": True,
                    "ready": False,
                }
            ),
            "",
        )

    monkeypatch.setattr(stats.subprocess, "run", run)
    readings = stats.collect()
    assert [sample.container for sample in readings] == ids[:2]
    assert readings[0].healthy is True and readings[0].ready is False
    assert not readings[1].counters
    assert len(commands) == 5
    output = capsys.readouterr()
    assert "skipped" in output.err
    assert "do-not-print" not in output.out + output.err


@pytest.mark.parametrize("duration", ["-1", "nan", "inf", "bad"])
def test_duration_validation(stats: ModuleType, duration: str) -> None:
    with pytest.raises(SystemExit) as error:
        stats.main(["--duration", duration])
    assert error.value.code == 2


def test_window_resamples_and_finishes_on_duration(
    stats: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = [0.0]
    calls = []

    def collect() -> list[object]:
        calls.append(clock[0])
        return []

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(stats, "collect", collect)
    monkeypatch.setattr(stats.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(stats.time, "sleep", sleep)
    assert stats.main(["--duration", "12", "--interval", "5"]) == 0
    assert calls == [0, 5, 10, 12]
    assert "SUMMARY" in capsys.readouterr().out


def test_snapshot_takes_two_samples_and_errors_never_expose_stderr(
    stats: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = [0.0]
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(clock[0])
        return subprocess.CompletedProcess(command, 0, "", "")

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(stats.subprocess, "run", run)
    monkeypatch.setattr(stats.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(stats.time, "sleep", sleep)
    assert stats.main(["--interval", "2"]) == 0
    assert calls == [0, 2]

    def fail(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "rtmp://host/live/private-token")

    monkeypatch.setattr(stats.subprocess, "run", fail)
    assert stats.main([]) == 1
    output = capsys.readouterr()
    assert "Error:" in output.err
    assert "private-token" not in output.out + output.err
    assert "Traceback" not in output.out + output.err


@pytest.mark.parametrize("service", ["streamer", "web"])
def test_sampler_reads_integer_counters_and_only_projects_health_booleans(
    stats: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    service: str,
) -> None:
    files = {
        "/sys/fs/cgroup/cpu.stat": "usage_usec 123456789\nuser_usec 110000000\n",
        "/sys/fs/cgroup/memory.current": "4096\n",
        "/sys/fs/cgroup/memory.stat": "inactive_file 1024\nactive_file 512\n",
        "/sys/fs/cgroup/io.stat": (
            "8:0 rbytes=1001 wbytes=2003 rios=1 wios=2\n8:16 rbytes=9 wbytes=7\n"
        ),
        "/proc/net/dev": (
            "header\nheader\n"
            " lo: 900 0 0 0 0 0 0 0 900 0 0 0 0 0 0 0\n"
            " eth0: 1000000001 0 0 0 0 0 0 0 2000000003 0 0 0 0 0 0 0\n"
        ),
    }
    health_reads = []

    def read_text(path: Path) -> str:
        return files[str(path)]  # Any unexpected file read fails this test.

    def health(directory: Path) -> dict[str, object]:
        health_reads.append(directory)
        return {"healthy": False, "ready": True, "private_field": "must-not-leak"}

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr("telegram_stream.health.read_status", health)
    monkeypatch.setattr(sys, "argv", ["sampler", service])
    monkeypatch.setattr(stats.time, "monotonic_ns", lambda: 999)
    exec(stats.SAMPLER, {})
    output = capsys.readouterr().out
    values = json.loads(output)
    assert values["counters"] == {
        "clock": 999,
        "cpu": 123456789000,
        "ram": 3072,
        "read": 1010,
        "write": 2010,
    }
    assert values["network"] == {"eth0": [1000000001, 2000000003]}
    assert values["healthy"] is (False if service == "streamer" else None)
    assert values["ready"] is (False if service == "streamer" else None)
    assert len(health_reads) == (1 if service == "streamer" else 0)
    assert "must-not-leak" not in output

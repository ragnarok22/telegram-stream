#!/usr/bin/env python3
"""Read-only Compose resource sampling, using Python and Linux cgroup v2 in containers.

Usage: python3 scripts/resource_stats.py [--duration 900] [--interval 10]
The default is a two-reading snapshot, five seconds apart. No files are written.
Docker's active context is respected; container names and daemon sockets are not assumed.
"""

import argparse
import json
import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSPECT = (
    '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"started":{{json .State.StartedAt}},"state":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}}'
)
# Only resource counters and the existing non-secret health file are read.
# Do not run the CLI's "status": it exposes more fields than this helper needs.
SAMPLER = r"""
import json
import os
import sys
import time
from pathlib import Path

group = Path("/sys/fs/cgroup")
def fields(name):
    return {key: int(value) for key, value in
            (line.split() for line in (group / name).read_text().splitlines())}

cpu = fields("cpu.stat")["usage_usec"] * 1000
ram = max(0, int((group / "memory.current").read_text())
          - fields("memory.stat").get("inactive_file", 0))
read = write = 0
for line in (group / "io.stat").read_text().splitlines():
    values = dict(item.split("=") for item in line.split()[1:])
    read += int(values.get("rbytes", 0))
    write += int(values.get("wbytes", 0))
network = {}
for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
    name, values = line.split(":", 1)
    if name.strip() != "lo":
        values = values.split()
        network[name.strip()] = [int(values[0]), int(values[8])]
clock = time.monotonic_ns()
healthy = ready = None
if sys.argv[1] == "streamer":
    try:
        from telegram_stream.health import read_status
        status = read_status(Path(os.environ.get("DATA_DIR", "./data")))
        healthy = status.get("healthy") is True
        ready = healthy and status.get("ready") is True
    except Exception:
        pass
print(json.dumps({"counters": {"clock": clock, "cpu": cpu, "ram": ram,
                              "read": read, "write": write},
                  "network": network, "healthy": healthy, "ready": ready}))
"""


class StatsError(Exception):
    """A deliberately non-sensitive diagnostic."""


@dataclass
class Sample:
    container: str
    service: str
    started: str
    state: str
    health: str
    counters: dict[str, int] = field(default_factory=dict)
    network: dict[str, list[int]] = field(default_factory=dict)
    healthy: bool | None = None
    ready: bool | None = None


@dataclass
class Summary:
    seconds: float = 0
    cpu: int = 0
    rx: int = 0
    tx: int = 0
    read: int = 0
    write: int = 0
    peak_ram: int = 0
    peak_cpu: float = 0


def docker(*args: str) -> str:
    try:
        result = subprocess.run(
            ["docker", *args],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StatsError("Docker unavailable or timed out.") from None
    if result.returncode:
        # Never forward Docker/Compose stderr: interpolation errors may contain secrets.
        raise StatsError("Docker read failed; check daemon/context and cgroup v2 support.")
    return result.stdout


def collect() -> list[Sample]:
    ids = docker("compose", "--profile", "web", "ps", "--all", "--quiet").split()
    if any(not re.fullmatch(r"[a-f0-9]{12,64}", item) for item in ids):
        raise StatsError("Unexpected Compose container IDs.")
    samples = []
    for container in dict.fromkeys(ids):
        try:
            metadata = json.loads(docker("inspect", "--format", INSPECT, container))
            service = metadata["service"]
            if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", service):
                raise ValueError
            sample = Sample(container=container, **metadata)
            if sample.state == "running":
                values = json.loads(
                    docker("exec", container, "python3", "-B", "-c", SAMPLER, service)
                )
                sample.counters = values["counters"]
                sample.network = values["network"]
                sample.healthy = values["healthy"]
                sample.ready = values["ready"]
            samples.append(sample)
        except (StatsError, ValueError, TypeError, KeyError):
            print(
                f"Warning: {container[:12]} unavailable or counters unsupported; skipped.",
                file=sys.stderr,
            )
    return samples


def delta(previous: Sample | None, current: Sample) -> dict[str, int] | None:
    """Discard gaps, restarts, interface changes and counter resets instead of guessing."""
    if (
        previous is None
        or (previous.container, previous.started) != (current.container, current.started)
        or not previous.counters
        or not current.counters
        or previous.network.keys() != current.network.keys()
    ):
        return None
    changes = {
        key: current.counters[key] - previous.counters[key]
        for key in ("clock", "cpu", "read", "write")
    }
    network = [
        [values[index] - previous.network[name][index] for index in (0, 1)]
        for name, values in current.network.items()
    ]
    if changes["clock"] <= 0 or min(changes.values()) < 0:
        return None
    if any(min(values) < 0 for values in network):
        return None
    changes["rx"] = sum(values[0] for values in network)
    changes["tx"] = sum(values[1] for values in network)
    return changes


def yes_no(value: bool | None) -> str:
    return "--" if value is None else ("yes" if value else "no")


def report(
    samples: list[Sample],
    previous: dict[str, Sample],
    summaries: dict[tuple[str, str], Summary],
) -> None:
    print(
        "SERVICE/ID  STATE  DOCKER-HEALTH  APP-HEALTH/READY  CPU%  RAM-MiB"
        "  NET-RX/TX-KiB/s  IO-R/W-KiB/s"
    )
    if not samples:
        print("No project containers available (web profile included).")
    for sample in samples:
        rates = "--  --"
        ram = "--"
        cpu = "--"
        change = delta(previous.get(sample.container), sample)
        if sample.counters:
            summary = summaries.setdefault((sample.service, sample.container), Summary())
            summary.peak_ram = max(summary.peak_ram, sample.counters["ram"])
            ram = f"{sample.counters['ram'] / 1024**2:.1f}"
            if change:
                seconds = change["clock"] / 1e9
                percent = change["cpu"] / change["clock"] * 100
                cpu = f"{percent:.1f}"
                rates = "  ".join(
                    f"{change[a] / seconds / 1024:.2f}/{change[b] / seconds / 1024:.2f}"
                    for a, b in (("rx", "tx"), ("read", "write"))
                )
                summary.seconds += seconds
                for key in ("cpu", "rx", "tx", "read", "write"):
                    setattr(summary, key, getattr(summary, key) + change[key])
                summary.peak_cpu = max(summary.peak_cpu, percent)
        print(
            f"{sample.service}/{sample.container[:12]}  {sample.state}  {sample.health}  "
            f"{yes_no(sample.healthy)}/{yes_no(sample.ready)}  {cpu}  {ram}  {rates}",
            flush=True,
        )


def summarize(summaries: dict[tuple[str, str], Summary]) -> None:
    print("\nSUMMARY (observed intervals only; gaps/restarts excluded)")
    for (service, container), total in summaries.items():
        if not total.seconds:
            print(f"{service}/{container[:12]}: no valid counter interval.")
            continue
        print(
            f"{service}/{container[:12]}: {total.seconds:.1f}s observed; "
            f"CPU avg/peak {total.cpu / total.seconds / 1e9 * 100:.1f}/{total.peak_cpu:.1f}%; "
            f"RAM peak {total.peak_ram / 1024**2:.1f} MiB; "
            f"NET avg RX/TX {total.rx / total.seconds / 1024:.2f}/"
            f"{total.tx / total.seconds / 1024:.2f} KiB/s; "
            f"NET total RX/TX {total.rx / 1024**2:.2f}/{total.tx / 1024**2:.2f} MiB; "
            f"IO total R/W {total.read / 1024**2:.2f}/{total.write / 1024**2:.2f} MiB"
        )


def number(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected finite seconds") from None
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("expected finite, non-negative seconds")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=number, default=0, help="window seconds; 0 = snapshot")
    parser.add_argument("--interval", type=number, default=5, help="sample seconds, minimum 1")
    args = parser.parse_args(argv)
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    summaries: dict[tuple[str, str], Summary] = {}
    try:
        samples = collect()
        previous: dict[str, Sample] = {}
        start = time.monotonic()
        deadline = start + (args.duration or args.interval)
        print("CPU 100% = one core; RAM excludes inactive file cache; loopback excluded.")
        print("Rates use exact byte deltas / monotonic elapsed time; -- = no valid interval.")
        while True:
            now = time.monotonic()
            print(f"\n[{now - start:.1f}s]")
            report(samples, previous, summaries)
            if now >= deadline:
                break
            previous = {sample.container: sample for sample in samples}
            time.sleep(max(0, min(now + args.interval, deadline) - time.monotonic()))
            samples = collect()
        return 0
    except KeyboardInterrupt:
        return 130
    except (StatsError, OSError, ValueError, TypeError, KeyError):
        print("Error: resource sampling failed; check Docker context and Compose.", file=sys.stderr)
        return 1
    finally:
        summarize(summaries)


if __name__ == "__main__":
    raise SystemExit(main())

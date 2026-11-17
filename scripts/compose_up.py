"""Start Compose services for the effective source; use --build to rebuild images."""

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError

READINESS_PROBE = (
    "import json; "
    "from telegram_stream.config import data_directory; "
    "from telegram_stream.health import read_status; "
    "status = read_status(data_directory()); "
    "print(json.dumps({key: status.get(key) for key in ('healthy', 'ready', 'last_tick')}))"
)


class ComposeError(Exception):
    """A safe diagnostic that does not include configuration or subprocess output."""


def run_compose(
    command: list[str], action: str, *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise ComposeError(f"Docker Compose could not {action}.") from None
    if result.returncode:
        raise ComposeError(f"Docker Compose could not {action} (exit code {result.returncode}).")
    return result


def service_environment(services: object, name: str) -> dict[str, str]:
    if not isinstance(services, dict) or not isinstance(services.get(name), dict):
        raise ComposeError(f"Docker Compose configuration must contain the {name} service.")
    environment = services[name].get("environment", {})
    if not isinstance(environment, dict) or any(
        not isinstance(value, str) and value is not None for value in environment.values()
    ):
        raise ComposeError(f"Docker Compose returned invalid {name} environment configuration.")
    # A null Compose environment value is absent inside the container.
    return {key: value for key, value in environment.items() if value is not None}


def effective_settings(output: str) -> Settings:
    try:
        configuration = json.loads(output)
    except json.JSONDecodeError:
        raise ComposeError("Docker Compose returned invalid JSON configuration.") from None
    if not isinstance(configuration, dict):
        raise ComposeError("Docker Compose configuration must contain the streamer service.")
    services = configuration.get("services")
    environment = service_environment(services, "streamer")
    try:
        settings = Settings.from_env(environment)
        if settings.kind == "web":
            from telegram_stream.web import WebSettings

            WebSettings.from_env(service_environment(services, "web"))
        return settings
    except FatalError as error:
        raise ComposeError(f"Invalid service configuration: {error}") from None
    except (OSError, ValueError):
        raise ComposeError("Invalid service configuration.") from None


def wait_for_streamer(compose: list[str], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    heartbeat: int | float | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = run_compose(
                [*compose, "exec", "-T", "streamer", "python", "-c", READINESS_PROBE],
                "check streamer readiness",
                timeout=min(5, remaining),
            )
            status = json.loads(result.stdout)
            if isinstance(status, dict):
                tick = status.get("last_tick")
                if type(tick) in (int, float):
                    # Require a heartbeat update after the first probe, not a persisted
                    # ready file left by a previous container. No host clock assumptions.
                    if heartbeat is None:
                        heartbeat = tick
                    elif (
                        tick != heartbeat
                        and status.get("healthy") is True
                        and status.get("ready") is True
                    ):
                        return
        except (ComposeError, ValueError):
            pass
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise ComposeError("Streamer did not become ready before the readiness timeout.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build", action="store_true", help="Build images before starting services."
    )
    parser.add_argument(
        "-f", "--file", action="append", default=[], help="Compose configuration file."
    )
    parser.add_argument(
        "--env-file", action="append", default=[], help="Compose interpolation file."
    )
    parser.add_argument("-p", "--project-name", help="Compose project name.")
    parser.add_argument("--project-directory", help="Compose project directory.")
    parser.add_argument(
        "--ready-timeout",
        type=int,
        default=300,
        help="Maximum publisher readiness wait, in seconds.",
    )
    args = parser.parse_args(argv)
    if args.ready_timeout <= 0:
        parser.error("--ready-timeout must be positive")

    # Keep identical project, interpolation, and profile settings for every call.
    # Compose itself handles .env, env_file, overrides, and COMPOSE_* variables.
    compose = ["docker", "compose"]
    for filename in args.file:
        compose.extend(["--file", filename])
    for filename in args.env_file:
        compose.extend(["--env-file", filename])
    if args.project_name:
        compose.extend(["--project-name", args.project_name])
    if args.project_directory:
        compose.extend(["--project-directory", args.project_directory])
    compose.extend(["--profile", "web"])
    up = ["up", "-d", "--no-deps"]
    if args.build:
        up.append("--build")

    try:
        configuration = run_compose(
            [*compose, "config", "--format", "json"], "read effective configuration"
        )
        settings = effective_settings(configuration.stdout)
        if settings.kind == "web":
            print("Waiting for healthy web service.", flush=True)
            run_compose([*compose, *up, "--wait", "--wait-timeout", "120", "web"], "start web")
        print("Starting streamer service.", flush=True)
        run_compose([*compose, *up, "streamer"], "start streamer")
        print("Waiting for ready streamer service.", flush=True)
        wait_for_streamer(compose, args.ready_timeout)
        if settings.kind != "web":
            # Do not remove the old browser until a fresh publisher is actually ready.
            print("Stopping unused web service.", flush=True)
            run_compose([*compose, "stop", "web"], "stop web")
    except ComposeError as error:
        # Compose diagnostics may include interpolated secrets, so never relay them.
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Start Compose services for the effective source; use --build to rebuild images."""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence


class ComposeError(Exception):
    """A safe diagnostic that does not include configuration or subprocess output."""


def run_compose(command: list[str], action: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except (OSError, UnicodeError):
        raise ComposeError(f"Docker Compose could not {action}.") from None
    if result.returncode:
        raise ComposeError(f"Docker Compose could not {action} (exit code {result.returncode}).")
    return result


def effective_source_type(output: str) -> str:
    try:
        configuration = json.loads(output)
    except json.JSONDecodeError:
        raise ComposeError("Docker Compose returned invalid JSON configuration.") from None
    if not isinstance(configuration, dict):
        raise ComposeError("Docker Compose configuration must contain the streamer service.")
    services = configuration.get("services")
    if not isinstance(services, dict) or not isinstance(services.get("streamer"), dict):
        raise ComposeError("Docker Compose configuration must contain the streamer service.")
    environment = services["streamer"].get("environment", {})
    if not isinstance(environment, dict):
        raise ComposeError("Docker Compose returned invalid streamer environment configuration.")
    # A null Compose environment value is absent inside the container.
    source_type = environment.get("SOURCE_TYPE")
    if source_type is None:
        return "auto"
    if not isinstance(source_type, str) or source_type.lower() not in {
        "auto",
        "youtube",
        "hls",
        "file",
        "web",
    }:
        raise ComposeError("Effective SOURCE_TYPE must be auto, youtube, hls, file, or web.")
    return source_type.lower()


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
    args = parser.parse_args(argv)

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
        source_type = effective_source_type(configuration.stdout)
        if source_type == "web":
            print("Waiting for healthy web service.", flush=True)
            run_compose([*compose, *up, "--wait", "--wait-timeout", "120", "web"], "start web")
        print("Starting streamer service.", flush=True)
        run_compose([*compose, *up, "streamer"], "start streamer")
        if source_type != "web":
            # Do not remove the old browser until the streamer starts successfully.
            print("Stopping unused web service.", flush=True)
            run_compose([*compose, "stop", "web"], "stop web")
    except ComposeError as error:
        # Compose diagnostics may include interpolated secrets, so never relay them.
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

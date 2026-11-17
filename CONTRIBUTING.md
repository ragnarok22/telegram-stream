# Contributing to telegram-stream

Thanks for helping improve telegram-stream. Bug reports, documentation fixes, and
focused code changes are welcome. Discuss substantial features in an issue before
starting work.

## Development setup

Install [uv](https://docs.astral.sh/uv/), Git, and Make. Python 3.12+ is supported;
development uses the Python 3.14 pin in [`.python-version`](.python-version).
Install FFmpeg and ffprobe for local streaming. Docker with Compose v2 is an
alternative for running the service.

From your cloned repository:

```sh
uv python install
make setup
```

`make setup` runs `uv sync --locked` to install the project and development tools.
The browser sidecar has a separate dependency group. Install it to work on the
sidecar or run `tests/test_web.py` as part of the test suite:

```sh
uv sync --locked --group browser
```

Running the sidecar locally also requires Chromium and Xvfb. See the
[README](README.md#local-development) for runtime setup and Docker alternatives.

Automated tests do not require Telegram credentials or publish live stories.
For manual streaming, follow the README's authentication instructions. The
application reads the process environment; it does not automatically load `.env`.

## Project layout

- `src/telegram_stream/cli.py`, `config.py`: commands and environment settings.
- `src/telegram_stream/lifecycle.py`, `telegram.py`, `state.py`: Live Story
  reconciliation, Telegram API access, and durable ownership state.
- `src/telegram_stream/service.py`, `health.py`, `retry.py`: supervision, health,
  and recovery.
- `src/telegram_stream/sources.py`, `ffmpeg.py`, `process.py`: source resolution,
  encoding, and subprocess management.
- `src/telegram_stream/web.py`: credential-free browser capture sidecar.
- `tests/`: pytest regression tests.
- `.github/workflows/ci.yml`: quality, test, and packaging checks.

## Making changes

- Keep changes focused and follow existing patterns. Avoid unrelated refactors.
- Use type annotations and syntax compatible with Python 3.12, even when
  developing on a newer interpreter.
- Follow `.editorconfig` and Ruff's configuration in `pyproject.toml`; Python
  lines are formatted to 100 characters.
- Do not suppress lint or type errors just to make checks pass.
- Update `README.md` and `.env.example` when changing behavior or configuration.
- When changing dependencies, update `pyproject.toml` and `uv.lock` together using
  uv. Do not edit the lockfile manually.

Preserve the service's safety guarantees:

- Publish **Telegram Channel Live Stories**, not traditional channel livestreams.
  Telegram schema changes must retain the required Live Story flags.
- Keep creation IDs durable before API calls and preserve explicit ownership
  checks. Do not silently adopt or replace an unknown story.
- Distinguish temporary network failures from confirmed story termination;
  respect FloodWait delays and stop the old publisher before replacing it.
- Keep subprocess cleanup, credential redaction, and session locking intact.
- Keep the browser sidecar isolated from Telegram credentials and session data.
  It supplies a private HLS feed, not a second Telegram publisher.

## Validation

Run checks appropriate to your change:

| Command | Purpose |
| --- | --- |
| `make format` | Format Python code with Ruff |
| `make check` | Check formatting, lint, and types; does not run tests |
| `make test` | Run all pytest tests |
| `make build` | Build source and wheel distributions |
| `make docker-build` | Build the runtime image |
| `make docker-dev` | Build the development image |

For fast feedback, run the affected tests directly:

```sh
uv run --locked pytest tests/test_lifecycle.py
uv run --locked --group browser pytest tests/test_web.py
```

For bug fixes, reproduce the problem first and add a focused regression test
when practical. Prioritize ownership, recovery, media progress, process cleanup,
and secret-handling risks. Reuse existing fakes and temporary-directory fixtures;
keep automated tests independent of Telegram and external media services.
Documentation-only changes generally do not need Python tests.

CI runs `make check`, `make test`, and `make build`. All required checks must pass.
For container changes, also validate the affected image and Compose configuration.

Real streaming acceptance tests must use a dedicated test channel and media you
have permission to distribute. Verify that output appears in Telegram's **story
bar**, that recovery works after a source failure or restart, and that only one
publisher and one active Live Story exist. Never test against a production
publisher's session or state volume.

## Publishing Docker images

The default Compose file uses published images. For local container development,
add `-f docker-compose.yml -f docker-compose.build.yml` to Compose commands; see
the [local build instructions](README.md#build-docker-images-locally).

After the release commit passes CI and contains the publishing workflow, push a
tag with two or three numeric components:

```sh
git tag v1.2.3
git push origin v1.2.3
```

Tags such as `v0.1`, `v12.34`, and `v0.1.0` trigger publishing; `v1`, `v1.2.3.4`,
and `v1.2-beta` do not. A `v1.2.3` tag publishes these Linux AMD64 images:

- `ghcr.io/ragnarok22/telegram-stream:v1.2.3` and `:latest` (main publisher).
- `ghcr.io/ragnarok22/telegram-stream:v1.2.3-web` and `:web` (browser sidecar).

The workflow uses the automatic `GITHUB_TOKEN` with `packages: write`; no
additional registry secret is needed. The `latest` and `web` aliases follow
publication order, not numerical version order. Tag pushes are not processed
retroactively: create a new version tag on a commit containing the workflow
if an earlier tag did not trigger publishing.

After the first successful publication, open the package's settings in GitHub
Packages and change its visibility to **Public** to allow unauthenticated pulls.

## Issues and pull requests

For bug reports, include:

- Expected and actual behavior, with minimal reproduction steps.
- Your revision, OS, Python or Docker version, source type, and relevant settings.
- Sanitized logs and whether the problem occurs locally, in Docker, or both.

For pull requests:

- Explain what changed and why; link a related issue when applicable.
- Describe the checks you ran and any manual verification or known limitations.
- Use concise commit subjects; existing history uses prefixes such as `feat:`,
  `fix:`, and `build:`, with optional scopes such as `fix(telegram):`.

Never commit or attach `.env`, Telegram sessions, API hashes, RTMP keys, cookies,
signed media URLs, or state-volume backups. Redact sensitive values from logs,
screenshots, and reproduction steps. Report security vulnerabilities privately
to the maintainer rather than publishing exploitable details in a public issue.

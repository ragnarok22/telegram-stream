# telegram-stream

[![CI][badge-ci]][ci]
[![Python 3.12+][badge-python]][python]
[![uv][badge-uv]][uv]
[![Ruff][badge-ruff]][ruff]
[![License: MIT][badge-license]](LICENSE)

Publish YouTube Live, HLS, looped local video, or a rendered web page to
**Telegram Channel Live Stories**, with supervised FFmpeg publishing and restart
recovery. Output appears in the channel's **story bar**, not as a traditional
channel livestream.

> [!IMPORTANT]
> Alpha software: recovery does not guarantee uninterrupted video or unlimited
> story duration. Telegram eligibility, rate limits, and source restrictions
> still apply. Test with a dedicated channel first.

## Quick start: Docker Compose

You need Docker Compose v2 and a Telegram **user account** with permission to
post stories in an eligible broadcast channel. Grant video-chat/live-stream
management where Telegram requires it. Bot tokens do not work.

**1. Clone and configure:**

```sh
git clone https://github.com/ragnarok22/telegram-stream.git
cd telegram-stream
cp .env.example .env
chmod 600 .env
mkdir -p media
```

Edit `.env` with your [Telegram API credentials](https://my.telegram.org/apps),
channel, and source:

```dotenv
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_32_character_api_hash
TELEGRAM_CHANNEL=@your_channel
SOURCE_TYPE=youtube
SOURCE=https://www.youtube.com/watch?v=YOUR_LIVE_VIDEO_ID
```

Replace the example values. `TELEGRAM_CHANNEL` also accepts a numeric `-100…`
peer ID. For other inputs, see [Sources](#sources).

**2. Authenticate, then start:**

```sh
docker compose build
docker compose run --rm streamer auth
docker compose up -d
```

`auth` asks for your phone number, login code, and optional 2FA password. The code
may arrive in an existing Telegram app. The saved session lives in the
`telegram-state` volume and grants access to your account: **never share it**.

**3. Verify the stream:**

```sh
docker compose exec streamer telegram-stream status
docker compose logs -f --tail=100 streamer
```

After startup, look for `"ready": true` and confirm the video appears in the
channel's **story bar**. Run only **one publisher deployment per channel**.

## Prebuilt Docker images

Tagged releases publish **Linux AMD64** images to
[GitHub Container Registry](https://github.com/ragnarok22/telegram-stream/pkgs/container/telegram-stream).
For example, pushing `v1.2` publishes:

| Image | Purpose |
| --- | --- |
| `ghcr.io/ragnarok22/telegram-stream:v1.2` | Main publisher (`runtime` target) |
| `ghcr.io/ragnarok22/telegram-stream:v1.2-web` | Optional browser sidecar (`web` target) |
| `ghcr.io/ragnarok22/telegram-stream:latest` | Most recently published main image |
| `ghcr.io/ragnarok22/telegram-stream:web` | Most recently published browser image |

### Use with Docker Compose

Clone and configure the project as in step 1 above, then create
`docker-compose.override.yml` beside `docker-compose.yml`:

```yaml
services:
  streamer:
    image: ghcr.io/ragnarok22/telegram-stream:v1.2
  web:
    image: ghcr.io/ragnarok22/telegram-stream:v1.2-web
```

Replace `v1.2` with an available release, keeping both images on the same version.
Compose loads this override automatically, preserving the existing volumes,
environment, and security settings. Instead of building locally:

```sh
docker compose pull streamer
docker compose run --rm streamer auth
docker compose up -d --no-build
```

For web capture, configure the [web source](#web-pages-optional-browser-sidecar),
then pull and start both services:

```sh
docker compose --profile web pull
docker compose --profile web up -d --no-build --wait
```

While using published images, use `pull` and `up --no-build`, not the `build` or
`up --build` commands shown elsewhere in this guide. To upgrade, update both
image tags in the override, pull again, and recreate with `up -d --no-build`
(include `--profile web` for web capture). Keep the existing state volume.

### Publish a release

After the release commit passes CI and contains the publishing workflow, push a
tag with two or three numeric components:

```sh
git tag v1.2.3
git push origin v1.2.3
```

Tags such as `v0.1`, `v12.34`, and `v0.1.0` trigger publishing; `v1`, `v1.2.3.4`,
and `v1.2-beta` do not. Image tags preserve the full version: `v0.1.0` publishes
`v0.1.0` and `v0.1.0-web`, along with the `latest` and `web` aliases.
The workflow uses the automatic `GITHUB_TOKEN` with
`packages: write`; no additional registry secret is needed. The `latest` and
`web` aliases follow publication order, not numerical version order.

Tag pushes are not processed retroactively. If a tag was pushed before this
filter was added, create a new version tag on a commit containing the updated
workflow.

After the first successful publication, open the package's settings in GitHub
Packages and change its visibility to **Public** to allow unauthenticated pulls.

## Sources

Set these values in `.env`. Only restream material you have permission to
distribute. `SOURCE_TYPE=auto` detects YouTube, other HTTP(S) media URLs, and local
paths; web pages require explicit `web`.

### YouTube Live

```dotenv
SOURCE_TYPE=youtube
SOURCE=https://www.youtube.com/watch?v=YOUR_LIVE_VIDEO_ID
```

The video must currently be live; a channel's `/live` URL also works. YouTube
may block datacenter IPs or require cookies/tokens. The service does not bypass
those restrictions; see [yt-dlp's guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube).

For extension-free cookie capture, run this **on the computer with your browser**:

```sh
docker compose build streamer
uv run --locked telegram-stream cookies capture --browser firefox --install
```

This asks for permission, exports only unexpired YouTube cookies, and installs
them privately in the Docker volume over stdin. Leave `YOUTUBE_COOKIES_FILE`
empty for managed cookies. It does not switch sources or interrupt a live stream.
Use `--browser chrome` for Chrome, and optionally `--check-url "https://…"` to test
a live YouTube source before replacing existing cookies.

See [YouTube cookie capture and checks](docs/youtube-cookies.md) for profiles,
local-file import, privacy, and troubleshooting.

### HLS / M3U8

```dotenv
SOURCE_TYPE=hls
SOURCE=https://example.com/live/playlist.m3u8
```

Prefer a stable HTTP(S) URL. Signed URLs stop working when their authorization
expires; arbitrary HLS tokens are not automatically renewed.

### Local files

Place a readable video in `media/`:

```dotenv
SOURCE_TYPE=file
SOURCE=/media/video.mp4
```

The file loops continuously. Missing audio becomes silence; playback starts
from the beginning after a publisher restart.

### Web pages (optional browser sidecar)

```dotenv
SOURCE_TYPE=web
SOURCE=https://reinierhernandez.com
VIDEO_WIDTH=720
VIDEO_HEIGHT=1280
```

```sh
docker compose --profile web up -d --build --wait
```

The sidecar captures the page in Chromium and supplies private HLS to the
publisher. **Video only:** no website audio, automatic scrolling, clicks, or
login. Animations and client-side updates continue running.

Open only trusted pages: Chromium runs with `--no-sandbox` inside the isolated
container. Never expose its HLS server or DevTools publicly. See
[SECURITY.md](SECURITY.md) for isolation details.

When switching back to another source, stop the sidecar with
`docker compose --profile web stop web`, or use the source-aware `make up`
command below. Disabling a Compose profile alone does not stop an existing sidecar.

## Configuration

See [`.env.example`](.env.example) for the complete reference. Common options:

| Setting | Default | Purpose |
| --- | --- | --- |
| `VIDEO_WIDTH`, `VIDEO_HEIGHT` | `1280`, `720` | Even output dimensions; aspect-preserving letterbox |
| `VIDEO_FPS` | `30` | Constant output frame rate |
| `VIDEO_BITRATE`, `AUDIO_BITRATE` | `2500k`, `128k` | H.264 / AAC bitrate |
| `ENCODER_PRESET` | `veryfast` | CPU/quality trade-off |
| `YOUTUBE_MAX_HEIGHT`, `YOUTUBE_MAX_FPS` | `720`, `30` | Limit input quality before downloading and decoding |
| `LIVE_STORY_CAPTION` | empty | Optional caption; omitted if Telegram rejects it |
| `END_LIVE_STORY_ON_SHUTDOWN` | `false` | End the owned story on graceful shutdown |

Apply `.env` changes with `docker compose up -d` (include `--profile web` before
`up` for web capture). `docker compose restart` does not reload environment
settings. Native runs must export settings into the process environment.

### Resource controls

With [uv][uv] installed, use the source-aware deployment helper after changing
the source or configuration:

```sh
make up ARGS="--build"
```

It reads the effective Compose configuration without printing credentials,
waits for the web sidecar to become healthy when needed, and starts the
publisher. For other source types, it stops an unused web sidecar only after
the publisher starts successfully. It does not force a publisher restart when
the image and configuration are unchanged. The helper also accepts `-f`,
`--env-file`, and `--project-name` through `ARGS`.

Compose limits each service to **2 CPUs and 2 GiB RAM** by default. Override
`STREAMER_CPU_LIMIT`, `STREAMER_MEMORY_LIMIT`, `WEB_CPU_LIMIT`, or
`WEB_MEMORY_LIMIT` for demanding workloads. These are guardrails, not reserved
resources or performance guarantees: tight limits can cause stalls or OOM
restarts. Measure after every change.

For web sources:

- `WEB_QUEUE_SIZE=16` bounds the raw capture queue (allowed range: 1–64 frames).
  Avoid large queues: full-resolution frames can retain several GiB of shared
  memory.
- `WEB_CAPTURE_FPS` defaults to `VIDEO_FPS`. Try `15` for slowly changing pages;
  it cannot exceed output FPS. The encoder duplicates frames as needed to
  preserve output FPS.
- `WEB_CAPTURE_SCALE=2` preserves high-density rendering. Try `1` for lighter,
  native-size capture; both modes respect Chromium's minimum viewport width.
  Changing scale can also change the page's responsive layout and text rendering.
- `WEB_VIDEO_COPY=false` keeps transcode as the default. Enable it only for the
  compatible project sidecar to avoid a second video encode; incompatible or
  unverifiable media still transcodes. Audio remains AAC, including silence
  when the source has no audio. The `ffmpeg_started` log reports `video_mode`.
  If a compatible copy still fails at runtime, disable the option; runtime
  failures do not automatically switch encoders.

Compose forwards `VIDEO_FPS` and `VIDEO_BITRATE` to the sidecar. Native browser
runs use `WEB_FPS` and `WEB_VIDEO_BITRATE` instead.

YouTube selection is capped at 720p/30 FPS by default. Raise
`YOUTUBE_MAX_HEIGHT`/`YOUTUBE_MAX_FPS` if the output needs more detail. These
limits affect the input format, not the configured output size. Formats with
unknown resolution or FPS are skipped rather than silently bypassing the caps.

Measure a baseline and compare changes over 10–15 minutes:

```sh
make stats
make stats ARGS="--duration 600 --interval 5"
```

CPU uses **100% = one core**. Network rates are calculated from byte-counter
deltas, not cumulative totals. Sampling requires cgroup v2 in the containers;
gaps and restarts are excluded from rate summaries. Also check publisher
readiness and the video in Telegram; low resource usage alone does not imply
a healthy stream.

## Health, logs, and troubleshooting

```sh
docker compose ps
docker compose exec streamer telegram-stream healthcheck
docker compose logs --tail=100 streamer
```

`healthy` means the supervisor is alive; `ready` also requires advancing video
and recent Telegram confirmation. Monitor readiness as well as liveness.
Docker does not restart a container solely because its healthcheck fails.

To reauthenticate, stop the publisher first:

```sh
docker compose stop streamer
docker compose run --rm streamer auth
docker compose up -d
```

| Symptom | Action |
| --- | --- |
| `CHAT_ADMIN_REQUIRED` / eligibility errors | Check user permissions and channel Live Story eligibility |
| Another process owns `DATA_DIR` | Stop the other publisher/auth process; do not delete lock files |
| Source retries / stalled video | Check source availability, access restrictions, upload bandwidth, and CPU |
| Web source retries / no browser feed | Start the `web` profile, inspect `docker compose logs web`, and check the page loads |

Unknown active stories are not automatically taken over. Stop the service and
verify the story in Telegram before deliberately using
`ADOPT_EXISTING_LIVE_STORY=true`; reset it to `false` and recreate the publisher
after recovery.

## Linux / Proxmox deployment and security

- For Proxmox, prefer a Debian/Ubuntu VM with Docker. Start with 2–4 vCPUs and
  2–4 GB RAM, then measure encoding load; browser capture needs extra resources.
- Default containers run as UID/GID **10001** and publish no host ports. Allow
  outbound DNS, HTTPS/MTProto, and Telegram's RTMP/RTMPS endpoint.
- Keep `.env`, cookies, sessions, and backups private. State volumes are not
  encrypted. Bind-mounted state directories must be private and writable by UID 10001.
- Stop before backing up; restore the whole state volume. **`docker compose down -v`
  destroys credentials and recovery state.**

See [SECURITY.md](SECURITY.md) for safeguards and private vulnerability reporting.

## Local development

For native runs on Linux/macOS, install [uv][uv] and FFmpeg/ffprobe. Python 3.12+
is supported; development is pinned to 3.14. Export the settings shown above
using your shell, with a host path for local files (`.env` is not loaded):

```sh
uv python install
uv sync --locked
uv run --locked telegram-stream auth
uv run --locked telegram-stream run
```

Local browser capture additionally needs Chromium/Xvfb. Run
`uv run --locked --group browser python -m telegram_stream.web` with `PAGE_URL`
and optional `WEB_WIDTH`/`WEB_HEIGHT`/`WEB_FPS`; set `WEB_STREAM_URL` on the
publisher to the sidecar's reachable HLS URL.

## Acknowledgments

The idea for streaming web pages was inspired by
[qvapay/tglive](https://github.com/qvapay/tglive).

## License

Released under the [MIT License](LICENSE).

[badge-ci]: https://github.com/ragnarok22/telegram-stream/actions/workflows/ci.yml/badge.svg?branch=main
[ci]: https://github.com/ragnarok22/telegram-stream/actions/workflows/ci.yml
[badge-python]: https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white
[python]: https://www.python.org/downloads/
[badge-uv]: https://img.shields.io/badge/dependencies-uv-DE5FE9?logo=uv&logoColor=white
[uv]: https://docs.astral.sh/uv/
[badge-ruff]: https://img.shields.io/badge/code%20style-Ruff-D7FF64?logo=ruff&logoColor=black
[ruff]: https://docs.astral.sh/ruff/
[badge-license]: https://img.shields.io/badge/License-MIT-blue.svg

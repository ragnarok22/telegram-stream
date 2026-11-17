# telegram-stream

[![CI][badge-ci]][ci]
[![Python 3.12+][badge-python]][python]
[![uv][badge-uv]][uv]
[![Ruff][badge-ruff]][ruff]
[![License: MIT][badge-license]](LICENSE)

Stream **YouTube Live, HLS, looped video files, or a web page** to
**Telegram Channel Live Stories**. Video appears in the channel's **story bar**,
not as a traditional channel livestream. FFmpeg publishing is supervised, with
restart recovery.

**Recommended setup:** use the published Docker images. Download two configuration
files, edit your settings, sign in to Telegram, and start. No Git clone, Python,
FFmpeg installation, or local image build is needed.

> [!IMPORTANT]
> Alpha software: recovery does not guarantee uninterrupted video or unlimited
> story duration. Telegram eligibility, rate limits, and source restrictions
> still apply. Test with a dedicated channel first.

[Quick start](#quick-start) · [Configuration](#configuration) ·
[Sources](#sources) · [Everyday commands](#everyday-commands) ·
[Troubleshooting](#troubleshooting) · [Local development](#local-development)

## Quick start

### Before you start

- Install **Docker with Compose v2** and **curl**.
- Published images target **Linux AMD64**. ARM hosts need AMD64 emulation or a
  [local build](#build-docker-images-locally).
- Use a **Telegram user account**, not a bot token. It must be allowed to post
  stories in an eligible broadcast channel. Grant video-chat/live-stream
  management where Telegram requires it.
- Get your **API ID and API hash** from [my.telegram.org/apps](https://my.telegram.org/apps).

### 1. Download the configuration

For a **new installation**, run:

```sh
mkdir telegram-stream && cd telegram-stream
curl -fsSL https://raw.githubusercontent.com/ragnarok22/telegram-stream/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/ragnarok22/telegram-stream/main/.env.example -o .env
chmod 600 .env
mkdir -p media
```

These commands download configuration files, not an installation script.
For an existing installation, [update the images](#update-the-images) instead;
do not overwrite your `.env`.

### 2. Set your Telegram account, channel, and source

Open `.env` in a text editor, for example `nano .env`. Start with these five
settings; leave the rest at their defaults:

```dotenv
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_CHANNEL=@your_channel
SOURCE_TYPE=youtube
SOURCE=https://www.youtube.com/watch?v=YOUR_LIVE_VIDEO_ID
```

| Setting | What to enter |
| --- | --- |
| `TELEGRAM_API_ID` | Numeric API ID from my.telegram.org, not your phone number |
| `TELEGRAM_API_HASH` | API hash from the same page, not a bot token |
| `TELEGRAM_CHANNEL` | Channel username (`@your_channel`) or numeric peer ID (`-100…`) |
| `SOURCE_TYPE` | `youtube`, `hls`, `file`, or `web`; `auto` also detects media URLs and local paths |
| `SOURCE` | A currently live YouTube URL, HLS URL, container file path, or web page URL |

For other inputs, use the examples under [Sources](#sources). Web pages also
require the `web` profile shown there.

### 3. Pull the image, sign in, and start

For YouTube, HLS, or a local file:

```sh
docker compose pull streamer
docker compose run --rm streamer auth
docker compose up -d
```

`auth` asks for your phone number, login code, and optional 2FA password. The code
may arrive in an existing Telegram app. You normally authenticate only once:
the session is saved in the `telegram-state` Docker volume.

> [!WARNING]
> The saved session grants access to your Telegram account. Never share it, and
> never run more than **one publisher deployment per channel**.

For a web page, authenticate with the same `auth` command, then follow the
[web source startup commands](#web-pages-optional-browser-sidecar).

### 4. Check that it is streaming

```sh
docker compose exec streamer telegram-stream status
docker compose logs -f --tail=100 streamer
```

Look for `"ready": true` in the status output and confirm the video appears in
the channel's **story bar**. Press `Ctrl+C` to leave the logs without stopping
the stream. If it is not ready, see [Troubleshooting](#troubleshooting).

## Configuration

Docker Compose loads `.env` automatically. The five settings above are enough to
start; [`.env.example`](.env.example) documents the remaining options.

| Setting | Default | Purpose |
| --- | --- | --- |
| `VIDEO_WIDTH`, `VIDEO_HEIGHT` | `1280`, `720` | Even output dimensions; aspect-preserving letterbox |
| `VIDEO_FPS` | `30` | Constant output frame rate |
| `VIDEO_BITRATE`, `AUDIO_BITRATE` | `2500k`, `128k` | H.264 / AAC bitrate |
| `ENCODER_PRESET` | `veryfast` | CPU/quality trade-off |
| `YOUTUBE_MAX_HEIGHT`, `YOUTUBE_MAX_FPS` | `720`, `30` | Limit input quality before downloading and decoding |
| `LIVE_STORY_CAPTION` | empty | Optional caption; omitted if Telegram rejects it |
| `END_LIVE_STORY_ON_SHUTDOWN` | `false` | End the owned story on graceful shutdown |

**After editing `.env`, run `docker compose up -d`.** For web capture, use
`docker compose --profile web up -d --wait`. `docker compose restart` does not
reload environment settings.

### Choose an image version

Compose uses the images already published to
[GitHub Container Registry](https://github.com/ragnarok22/telegram-stream/pkgs/container/telegram-stream):

| Default image | Purpose |
| --- | --- |
| `ghcr.io/ragnarok22/telegram-stream:latest` | Main publisher |
| `ghcr.io/ragnarok22/telegram-stream:web` | Optional browser sidecar |

For a repeatable deployment, pin **both images to the same release** in `.env`.
For example:

```dotenv
STREAMER_IMAGE=ghcr.io/ragnarok22/telegram-stream:v0.1.1
WEB_IMAGE=ghcr.io/ragnarok22/telegram-stream:v0.1.1-web
```

Use an available version from the registry, then [pull and recreate](#update-the-images).
No Compose override file is needed.

## Sources

Set `SOURCE_TYPE` and `SOURCE` in `.env`, then recreate the services.
Only restream material you have permission to distribute.

### YouTube Live

```dotenv
SOURCE_TYPE=youtube
SOURCE=https://www.youtube.com/watch?v=YOUR_LIVE_VIDEO_ID
```

The video must currently be live; a channel's `/live` URL also works. YouTube
may block datacenter IPs or require cookies/tokens. The service does not bypass
those restrictions. See [YouTube cookie capture and checks](docs/youtube-cookies.md)
for optional host-browser capture, import, and troubleshooting.

### HLS / M3U8

```dotenv
SOURCE_TYPE=hls
SOURCE=https://example.com/live/playlist.m3u8
```

Prefer a stable HTTP(S) URL. Use explicit `hls` for extensionless manifests.
Signed URLs stop working when their authorization expires; arbitrary HLS tokens
are not automatically renewed.

### Local files

Place a readable video in the `media/` directory created during setup:

```dotenv
SOURCE_TYPE=file
SOURCE=/media/video.mp4
```

Use the **container path** `/media/…`, not the host's absolute path. The file
loops continuously. Missing audio becomes silence; playback starts from the
beginning after a publisher restart.

### Web pages (optional browser sidecar)

```dotenv
SOURCE_TYPE=web
SOURCE=https://reinierhernandez.com
VIDEO_WIDTH=720
VIDEO_HEIGHT=1280
```

After authenticating, pull and start **both** services:

```sh
docker compose --profile web pull
docker compose --profile web up -d --wait
```

The sidecar captures the page in Chromium and supplies private HLS to the
publisher. **Video only:** no website audio, automatic scrolling, clicks, or
login. Animations and client-side updates continue running.

Open only trusted pages: Chromium runs with `--no-sandbox` inside the isolated
container. Never expose its HLS server or DevTools publicly.

When switching back to YouTube, HLS, or a file, apply your `.env` changes with
`docker compose up -d`, then run `docker compose --profile web stop web`.
Disabling a Compose profile alone does not stop an existing sidecar.

## Everyday commands

Run these from your installation directory:

| Task | Command |
| --- | --- |
| Follow publisher logs | `docker compose logs -f --tail=100 streamer` |
| Check stream readiness | `docker compose exec streamer telegram-stream status` |
| List containers and health | `docker compose ps` |
| Apply `.env` changes / start | `docker compose up -d` |
| Apply changes with web capture | `docker compose --profile web up -d --wait` |
| Stop all services, keeping saved state | `docker compose --profile web down` |

**Never use `docker compose down -v` unless you intend to delete the saved
credentials and recovery state.**

### Update the images

If you pinned a release, change both image tags in `.env` first. Otherwise,
these commands fetch the current `latest` / `web` aliases.

For YouTube, HLS, or local files:

```sh
docker compose pull streamer
docker compose up -d
```

For web capture:

```sh
docker compose --profile web pull
docker compose --profile web up -d --wait
```

Keep the same directory, Compose project name, and state volume so the saved
session and recovery state are reused. You do not need to authenticate again.

## Troubleshooting

```sh
docker compose ps
docker compose exec streamer telegram-stream healthcheck
docker compose logs --tail=100 streamer
```

`healthy` means the supervisor is alive; `ready` also requires advancing video
and recent Telegram confirmation. Monitor readiness as well as liveness.
Docker does not restart a container solely because its healthcheck fails.

| Symptom | Action |
| --- | --- |
| `CHAT_ADMIN_REQUIRED` / eligibility errors | Check user permissions and channel Live Story eligibility |
| Another process owns `DATA_DIR` | Stop the other publisher/auth process; do not delete lock files |
| Source retries / stalled video | Check source availability, access restrictions, upload bandwidth, and CPU |
| Web source retries / no browser feed | Start the `web` profile, inspect `docker compose logs web`, and check the page loads |
| `no matching manifest` on an ARM host | Use AMD64 emulation or build images locally |

To reauthenticate, **stop the publisher first**:

```sh
docker compose stop streamer
docker compose run --rm streamer auth
docker compose up -d
```

Unknown active stories are not automatically taken over. Stop the service and
verify the story in Telegram before deliberately using
`ADOPT_EXISTING_LIVE_STORY=true`; reset it to `false` and recreate the publisher
after recovery.

## Deployment, resources, and security

- For Linux / Proxmox, prefer a Debian/Ubuntu VM with Docker. Start with
  **2–4 vCPUs and 2–4 GB RAM**, then measure; browser capture needs extra resources.
- Compose limits **each service to 2 CPUs and 2 GiB RAM** by default. See
  [resource tuning and deployment helpers](docs/operations.md) for overrides,
  lighter browser capture, and measurement.
- Containers run as UID/GID **10001** and publish no host ports. Allow outbound
  DNS, HTTPS/MTProto, and Telegram's RTMP/RTMPS endpoint.
- Keep `.env`, cookies, sessions, and backups private. State volumes are not
  encrypted. Bind-mounted state directories must be private and writable by UID 10001.
- Stop services before backing up; restore the whole state volume.

See [SECURITY.md](SECURITY.md) for isolation details and private vulnerability reporting.

## Local development

Only clone the repository if you want to develop, build images, or use the
host-side Python helpers:

```sh
git clone https://github.com/ragnarok22/telegram-stream.git
cd telegram-stream
cp .env.example .env
chmod 600 .env
mkdir -p media
```

Configure `.env` as in the quick start.

### Build Docker images locally

Use the explicit build override instead of the published images:

```sh
docker compose -f docker-compose.yml -f docker-compose.build.yml build streamer
docker compose -f docker-compose.yml -f docker-compose.build.yml run --rm streamer auth
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d
```

For web capture, build and start both targets:

```sh
docker compose -f docker-compose.yml -f docker-compose.build.yml --profile web up -d --build --wait
```

Keep using both `-f` arguments for subsequent commands on a locally built
deployment. To use published images again, omit the build override and pull them.

### Run without Docker

Install [uv][uv] and FFmpeg/ffprobe. Python 3.12+ is supported; development is
pinned to 3.14. Export your settings into the process environment using your
shell, with a host path for local files. **Native runs do not load `.env`.**

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for checks and publishing release images.

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

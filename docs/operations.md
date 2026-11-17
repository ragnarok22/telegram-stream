# Resource tuning and deployment helpers

For the recommended two-file setup using published images, start with the
[README](../README.md#quick-start). The Python helpers below are optional and
require a repository checkout and [uv](https://docs.astral.sh/uv/).

## Source-aware startup

From the repository root, after configuring `.env` and authenticating:

```sh
make up
```

This uses the published images by default. To build local images instead:

```sh
make up ARGS="-f docker-compose.yml -f docker-compose.build.yml --build"
```

The helper validates the effective publisher and web configuration without
printing credentials before changing services, waits for the web sidecar to
become healthy when needed, and starts the publisher. It then requires a fresh
heartbeat with both `healthy` and `ready` true; persisted status from an older
publisher is not enough. For other source types, it stops an unused web sidecar
only after that check succeeds.

It does not force a publisher restart when the image and configuration are
unchanged or pull a newer image automatically. Use the README's
[image update commands](../README.md#update-the-images) when upgrading.
The helper accepts `-f`, `--env-file`, and `--project-name` through `ARGS`.
Publisher readiness times out after 300 seconds by default; override it with
`--ready-timeout SECONDS`.

## CPU and memory limits

Compose limits each service to **2 CPUs and 2 GiB RAM** by default. Override
`STREAMER_CPU_LIMIT`, `STREAMER_MEMORY_LIMIT`, `WEB_CPU_LIMIT`, or
`WEB_MEMORY_LIMIT` in `.env` for demanding workloads.

These are guardrails, not reserved resources or performance guarantees: tight
limits can cause stalls or OOM restarts. Measure after every change.

## Browser capture

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

## YouTube input quality

YouTube selection is capped at 720p/30 FPS by default. Raise
`YOUTUBE_MAX_HEIGHT`/`YOUTUBE_MAX_FPS` if the output needs more detail. These
limits affect the input format, not the configured output size. Formats with
unknown resolution or FPS are skipped rather than silently bypassing the caps.

## Measure resource usage

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

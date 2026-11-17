# YouTube cookies without browser extensions

Use your **own signed-in browser on your computer**, not the web-capture sidecar
or a headless Google login. A dedicated normal Firefox/Chrome profile is
recommended. Native extraction does not capture private/incognito sessions.

## Capture and install

Capture needs a repository checkout and [uv](https://docs.astral.sh/uv/) on the
computer with your signed-in browser. The two-file Docker installation alone
does not include this host-side command. Run from the project root, using the
same Compose project and state volume as your publisher:

```sh
docker compose pull streamer
uv run --locked telegram-stream cookies capture --browser firefox --install
```

Pulling the helper image does not restart an existing publisher.
Capture runs on the host with your browser, asks for confirmation, and never
asks for your Google password. macOS may separately request Keychain access.

For Chrome use `--browser chrome`; `--profile Default` or `--profile "Profile 1"`
can select a Chrome profile directory. Firefox also accepts an explicit profile
directory path. Close the browser if its cookie database is locked. Do not run
capture with `sudo`. Automation requires explicit `--yes`.

The native yt-dlp reader may read/copy the browser's full cookie database locally
to decrypt it. The application exports **only unexpired YouTube-domain cookies**,
not Google account cookies from unrelated sites. It never prints cookie values.

With `--install`, filtered cookies go to a temporary Compose helper over stdin,
not command-line arguments or a host browser mount. The helper atomically writes
a mode-0600 file at `/data/youtube-cookies.txt` in the existing state volume.
No manual `chown`, mount, or extension is required.

Leave `YOUTUBE_COOKIES_FILE` empty for this managed path. Import does not contact
Telegram, acquire its session lock, switch `SOURCE`, or restart the publisher.
A YouTube publisher picks up a changed cookie file on its next source resolution.

## Verify before replacing existing cookies

Provide a **currently live** YouTube URL:

```sh
uv run --locked telegram-stream cookies capture --browser firefox --install \
  --check-url "https://www.youtube.com/watch?v=YOUR_LIVE_VIDEO_ID"
```

This stages the candidate and probes extraction and media access from the
server's actual IP. Failed validation leaves the previous cookies untouched.
It never starts a Telegram broadcast.

Check stored metadata or test an installed set:

```sh
docker compose run --rm --no-deps -T streamer cookies status
docker compose run --rm --no-deps -T streamer cookies check \
  --url "https://www.youtube.com/watch?v=YOUR_LIVE_VIDEO_ID"
```

Status shows counts, expired/session-cookie information, and whether known login
cookie names are present. **That is not proof of authenticated access.** A passed
online check means the source was accessible at that moment, not that cookies
will remain valid indefinitely or bypass provider restrictions.

## File export/import

If you prefer a private Netscape file:

```sh
uv run --locked telegram-stream cookies capture --browser firefox \
  --output secrets/youtube-cookies.txt
docker compose run --rm --no-deps -T streamer cookies import \
  < secrets/youtube-cookies.txt
```

For a native, non-Docker publisher use `--output data/youtube-cookies.txt` instead:
the default `DATA_DIR=./data` discovers it automatically.

Trusted extension exports can also be imported. JSON, malformed, unrelated-site,
and wholly expired files are rejected. Unrelated/expired cookies in mixed valid
files are removed. Files are size-limited and values never appear in errors.

Existing read-only mounts still work as an explicit override:

```yaml
services:
  streamer:
    volumes:
      - ./secrets/youtube-cookies.txt:/run/secrets/youtube-cookies.txt:ro
```

Set `YOUTUBE_COOKIES_FILE=/run/secrets/youtube-cookies.txt` for that mode and ensure
UID 10001 can read the file without making it public. Clear the override before
using managed imports. Each extraction uses a private filtered copy so yt-dlp
cannot mutate the mounted or managed original.

## Limits and security

- Treat cookies as passwords: never paste them into chats, commit them, or attach
  them to issues. Protect files and volume backups.
- Cookie capture does not automate Google login, MFA, CAPTCHA, or reauthentication.
  Log in yourself using your normal browser and recapture when needed.
- Profiles, browser encryption, keychain permissions, cookie rotation, IP blocks,
  and PO-token requirements can still prevent extraction. Cookies are not a
  universal remedy for YouTube restrictions.
- A dedicated profile that stays closed after capture may reduce rotation caused
  by normal browsing, but validity is not guaranteed.
- Using an account with yt-dlp can lead to account restrictions. Consider a
  dedicated account and only restream material you have permission to distribute.

See [yt-dlp's YouTube guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube)
and [SECURITY.md](../SECURITY.md).

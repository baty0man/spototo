# 🎵 Spototo

Turn Spotify playlists into self-updating [Yoto](https://yotoplay.com) Make-Your-Own
cards — complete with custom AI-generated pixel art icons for every song.

Name a playlist `Yoto-Bedtime`, add songs to it, and Spototo automatically:

- downloads new tracks as they're added
- generates a 16×16 pixel-art icon for each one (with a little AI help — see below)
- appends it as a chapter on a Yoto card called **"Bedtime"** (created for you on the first track)
- removes the chapter again if you delete the song from the playlist

Name another playlist `Yoto-Dinosaurs` and you get a second, independent card.
No per-playlist configuration — just the naming convention.

> ⚠️ **Audio comes from YouTube search results via `yt-dlp`**, not licensed
> Spotify streams — there's no way around this without label deals. It's a
> personal/family tool, not a distribution service. See [Legal](#legal--scope) below.

## How it works

```
Spotify (Yoto-* playlists)
        │  poll every few min, user OAuth
        ▼
spotify_sync/   (Docker, or a host systemd timer)
        │  yt-dlp download → data/watch/<card name>/
        ▼
src/            (Docker container)
        │  title → LLM subject → N candidate icons → vision judge picks best
        │  audio upload + transcode
        ▼
Yoto API  →  MYO card chapter
```

### Icon generation, the part that took the most tuning

This is the default (`ICON_SOURCE=generate`); if you'd rather have no AI
involved at all, skip to [the non-AI option](#dont-want-ai-involved-at-all-icon_sourcenone-or-yoto-library) below.

Feeding a raw song title straight to an image model produces mush — the two
things that actually work:

1. **Distill the title to one concrete subject first.** A small LLM (Llama 3
   8B) turns *"Candy Cane Crush!"* into *"candy cane"*, *"Gorilla Smash!"*
   into *"gorilla"*. The image model draws a clear object beautifully; it
   draws a compound song title as noise.
2. **Generate several candidates and judge them.** Any single generation is a
   coin flip — some come out clean, some come out mush. Spototo generates
   several candidates natively at 16×16 (the Yoto screen's real resolution;
   generating bigger and downscaling just moves the mush to a different step)
   and a vision model picks whichever one most clearly reads as the subject.

| ![Baby Shark](showcase/Baby%20Shark.png) | ![Gorilla Smash](showcase/Gorilla%20Smash.png) | ![Chug Like a Train](showcase/Chug%20Like%20a%20Train.png) |
|:---:|:---:|:---:|
| ![Dinosaur Dance](showcase/Dinosaur%20Dance.png) | ![Twinkle Twinkle](showcase/Twinkle%20Twinkle.png) | ![Robot Dance](showcase/Robot%20Dance.png) |
| ![Zoom to the Moon](showcase/Zoom%20to%20the%20Moon.png) | ![Baby Beluga](showcase/Baby%20Beluga.png) | ![Candy Cane](showcase/Candy%20Cane.png) |

Results are hit-and-miss — concrete nouns (shark, gorilla, train) come out
clean almost every time; abstract titles are more of a gamble — but usually
good enough that a kid can tell chapters apart by the icon.

**Cost is a dial, not fixed.** Each candidate icon is about $0.015 on
Replicate; the LLM subject-extraction and the judging pass are effectively
free by comparison. So cost per song ≈ `ICON_CANDIDATES × $0.015`:

| `ICON_CANDIDATES` | ~cost/song | quality |
|---|---|---|
| 8 (default) | ~$0.12 | most reliable |
| 4 | ~$0.06 | reliably good for concrete subjects |
| 1 | ~$0.015 | single-shot, no best-of |

Set it in `.env` — no rebuild needed, just a container restart.

### Don't want AI involved at all? `ICON_SOURCE=none` or `yoto-library`

The generative pipeline above is the default, but it's a config choice, not
a requirement:

- **`ICON_SOURCE=yoto-library`** — matches the song title against Yoto's own
  official icon library by keyword/tag, entirely deterministically. No LLM,
  no image generation, zero AI calls. If nothing matches well, the chapter
  just has no custom icon — it never silently falls back to generating one.
- **`ICON_SOURCE=none`** — skip the icon step completely.

With either of these set, `REPLICATE_API_TOKEN` isn't even needed.

### Library first, optional AI: `ICON_SOURCE=yoto-library-ai`

This mode tries the official library first. A library **no-match** may use AI
only when both `REPLICATE_API_TOKEN` and a positive `ICON_AI_MAX_GENERATIONS`
are configured. The cap defaults to **0**, so hybrid mode is library-only until
you opt in. A library/provider failure never triggers paid fallback; icon
failures leave the chapter without an icon and do not stop audio processing.
Unknown modes and negative/noninteger caps are rejected.

```dotenv
ICON_SOURCE=yoto-library-ai
ICON_AI_MAX_GENERATIONS=0
# ICON_CACHE_DIR=data/icons
```

`ICON_AI_MAX_GENERATIONS` is a **lifetime attempt allowance in this cache, not
a monetary budget**. An attempt includes subject extraction, `ICON_CANDIDATES`
images and potentially a vision judge. Pricing varies. Raising the cap permits
additional attempts; restarting does not reset it. Legacy `generate` still
allows generation without this hybrid cap; use hybrid mode for the guard.

The owner-only `ICON_CACHE_DIR` (default `data/icons`) stores an atomic ledger,
PNGs and uploaded media IDs. Keys include normalized title and model/style/
candidate/prompt settings, not the filename. Attempts are reserved durably
**before paid work**, including failed/uncertain attempts. Such attempts without
a completed PNG never automatically retry generation. A failed upload reuses
the saved PNG on retry without paying for generation again. Successfully
uploaded IDs are reused across restarts. Preserve/back up this cache: deleting
it deletes lifetime accounting too. Do not edit the ledger to reset spending;
corrupt accounting fails closed. Cache paths are local trusted configuration.

### Backfill missing icons without re-uploading audio

**Stop the main Spototo pipeline first** (`docker compose stop spototo`). The
pipeline and backfill hold the same nonblocking sibling lock for `STATE_FILE`
(e.g. `data/state.json.lock`), including token updates and card writes. Never
remove an active lock file, and use the same state/cache paths for both runs.
Leave the pipeline stopped until the maintenance command finishes.

```bash
# Preview only: no icon resolution, uploads, or card writes
python src/backfill_icons.py --card-id ID
# Explicit opt-in: fill missing chapter/track icons only, preserving existing icons
python src/backfill_icons.py --card-id ID --apply
```

Run with the project's Python dependencies and existing Yoto authentication.
Backfill never uploads audio; hybrid token/cap rules still apply. Before each
card write it saves a private original snapshot in `data/icon-backfill-backups`
(or `--backup-dir PATH`), fsyncs the file and directory entries (including newly
created backup directories), checks for concurrent card changes and verifies the
updated chapters by reading them back. A backup/fsync failure prevents the card
write. The shared lock serializes only local processes using that lock, not
remote editors. The final GET and POST are not atomic: without conditional
writes, a remote edit between them can be overwritten. Avoid editing the card
during maintenance. Inspect any reported conflict or failed verification before
proceeding. Cache, backups and lock byproducts are ignored by Git. Restart the
pipeline after maintenance (`docker compose start spototo`).

### Deletion is safety-scoped

Delete a song from the playlist and its chapter is removed from the Yoto
card too — but **only** chapters Spototo itself created and tracked. It will
never touch or guess about a chapter added any other way, even on a card you
also edit by hand.

## Components

| Path | What it does | Runs as |
|---|---|---|
| `spotify_sync/` | Discovers `Yoto-*` playlists, downloads new tracks, tracks deletions | Docker (`spototo-sync`) or host systemd timer |
| `src/` | Watches the download folder, generates icons, talks to the Yoto API | Docker (`spototo`) |
| `spotify_sync/spotify_auth_setup.py` | One-time Spotify OAuth login (needs a browser) | run manually, once |
| `auth_setup.py` | One-time Yoto OAuth login (needs a browser) | run manually, once |

Everything runs in Docker — no systemd, no Python venvs, no `apt install`.
It's been developed and run in production on Linux; it *should* work the
same via Docker Desktop on Windows and macOS since nothing in the containers
is Linux-specific, but that hasn't been verified firsthand yet. If you try it
on Windows/macOS, an issue report either way (works / doesn't) is welcome.

## Setup

```bash
cp .env.example .env
nano .env   # YOTO_CLIENT_ID, REPLICATE_API_TOKEN, SPOTIPY_CLIENT_ID/SECRET
```

Two one-time logins need an actual browser, so run them wherever you have
one — your laptop, not necessarily the server. Clone or copy the repo there,
then:

```bash
pip install -r requirements.txt -r spotify_sync/requirements.txt

# 1. Yoto login -> writes data/state.json
python auth_setup.py --client-id YOUR_YOTO_CLIENT_ID

# 2. Spotify login (needs playlist-read-private to list your playlists) -> writes data/spotify_token.json
python spotify_sync/spotify_auth_setup.py
```

Copy both resulting files to the machine that will actually run the
containers (which can be completely headless):

```bash
scp data/state.json data/spotify_token.json you@server:/path/to/spototo/data/
```

Then, on that server:

```bash
docker compose up --build -d
docker compose logs -f
```

Rename or create a Spotify playlist as `Yoto-<name>` and wait for the next
sync (every 5 min by default — see `SPOTIFY_POLL_SECONDS` in `.env.example`).

<details>
<summary>Prefer a host-level systemd timer instead of the spototo-sync container? (Linux only)</summary>

⚠️ Pick **one** — the Docker `spototo-sync` service and this systemd timer both
poll and download independently. Running both against the same `data/`
directory means duplicate, racing downloads. If you use this path, don't
also start the `spototo-sync` container (`docker compose up` without listing
it by name brings up every service, so start `spototo` explicitly instead).

```bash
docker compose up --build -d spototo   # just this one service

python3 -m venv spotify_sync/.venv
source spotify_sync/.venv/bin/activate
pip install -r spotify_sync/requirements.txt

# Edit systemd/spotify-sync.service first: it has placeholder paths/user
# (User=, WorkingDirectory=, EnvironmentFile=, ExecStart=) that need to match
# your actual username and where you cloned this repo.
sudo cp systemd/spotify-sync.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now spotify-sync.timer
```

Requires `ffmpeg` and Node.js on the host (used by `yt-dlp`). See `spotify_sync/README.md`.
</details>

## Requirements

- A [Yoto](https://yoto.dev) developer account (public client)
- A [Replicate](https://replicate.com) API token (LLM + image generation + vision judge)
- A Spotify Developer app (`playlist-read-private` scope)
- Docker (Docker Desktop on Windows/Mac, Docker Engine on Linux)

## Legal / scope

This is a **personal, single-user tool** — one Spotify account, one Yoto
account, run on your own hardware. It is **not** designed to be hosted as a
multi-tenant service on other people's behalf; doing so raises real copyright
and platform-ToS exposure that a personal self-hosted instance doesn't
(Spotify's Developer Terms explicitly prohibit using their API to enable
downloading of audio, and centralizing the download/redistribution step for
other users is a materially different legal posture than each person running
their own instance — see e.g. *A&M Records v. Napster*, which established
that "we don't charge for it" doesn't shield an operator of centralized
infrastructure that facilitates unlicensed copying at scale). Run your own
instance under your own accounts.

## Known limitations

- Single Spotify account, single Yoto account — see Legal above.
- Audio quality is bounded by whatever YouTube has for a given track.
- Offline resolver/backfill tests use `PYTHONPATH=src python -m unittest discover -s tests -v`.

## License

[MIT](LICENSE)

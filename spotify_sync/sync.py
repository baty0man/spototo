#!/usr/bin/env python3
"""
Discovers every Spotify playlist named `<prefix><name>` (default prefix
"Yoto-") in your account and downloads any not-yet-seen track into a
per-playlist folder under WATCH_DIR (data/watch/<name>/). The Yoto pipeline
watches those folders and turns each into its own MYO card named <name>.

Designed to be run periodically by a systemd timer. Downloads one track at a
time; a failure on one track is logged and skipped so it doesn't block the
rest. Idempotent: re-running skips any track ID already recorded per-playlist
in SPOTIFY_STATE_FILE.

Requires user OAuth (listing your playlists by name needs it). Run
spotify_auth_setup.py once on a machine with a browser and copy the resulting
data/spotify_token.json here.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import spotipy  # noqa: E402
from spotipy.oauth2 import SpotifyOAuth  # noqa: E402
from spotipy.cache_handler import CacheFileHandler  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spotify-sync")

SPOTIPY_CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID", "")
SPOTIPY_CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "")
PLAYLIST_PREFIX = os.environ.get("SPOTIFY_PLAYLIST_PREFIX", "Yoto-")
REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPE = "playlist-read-private"

WATCH_DIR = Path(os.environ.get("WATCH_DIR", REPO_ROOT / "data" / "watch")).expanduser()
# spotify_dl creates nested subfolders; download into staging OUTSIDE the watch
# tree so the pipeline's watcher only ever sees complete, correctly-placed files.
STAGING_DIR = Path(
    os.environ.get("SPOTIFY_STAGING_DIR", REPO_ROOT / "data" / ".staging")
).expanduser()
STATE_FILE = Path(
    os.environ.get("SPOTIFY_STATE_FILE", REPO_ROOT / "data" / "spotify_state.json")
).expanduser()
TOKEN_CACHE = os.environ.get(
    "SPOTIFY_TOKEN_CACHE", str(REPO_ROOT / "data" / "spotify_token.json")
)


def sanitize(name: str) -> str:
    """Filesystem-safe single-segment folder name."""
    return name.replace("/", "-").replace("\\", "-").strip() or "Unnamed"


def load_state() -> dict:
    """State shape: {"playlists": {<playlist_id>: {"name","card_name","downloaded":{...}}}}."""
    if not STATE_FILE.exists() or STATE_FILE.stat().st_size == 0:
        return {"playlists": {}}
    with open(STATE_FILE, "r") as f:
        data = json.load(f)
    data.setdefault("playlists", {})  # tolerate/migrate the old flat {"downloaded": {}}
    return data


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


def discover_playlists(sp: spotipy.Spotify):
    """All of the user's playlists whose name starts with PLAYLIST_PREFIX."""
    matched = []
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results.get("items", []):
            name = (pl or {}).get("name") or ""
            if name.startswith(PLAYLIST_PREFIX) and pl.get("id"):
                matched.append({"id": pl["id"], "name": name})
        results = sp.next(results) if results.get("next") else None
    return matched


def fetch_playlist_tracks(sp: spotipy.Spotify, playlist_id: str):
    tracks = []
    results = sp.playlist_items(playlist_id, additional_types=("track",))
    while results:
        for item in results.get("items", []):
            # Spotify returns the object under "item" on current API versions and
            # under "track" on older ones; accept either.
            track = item.get("track") or item.get("item")
            if not isinstance(track, dict) or not track.get("id"):
                continue  # local files / unavailable tracks have no id
            tracks.append(
                {
                    "id": track["id"],
                    "title": track["name"],
                    "artist": ", ".join(a["name"] for a in track.get("artists", [])),
                }
            )
        results = sp.next(results) if results.get("next") else None
    return tracks


def download_track(track: dict, card_name: str) -> str:
    """Download one track via spotify_dl into a staging dir, then move the
    finished MP3 into WATCH_DIR/<card_name>/. Returns the final filename.

    The filename is prefixed with the Spotify track ID (`<id>--<name>.mp3`)
    so the Yoto pipeline can record which chapter came from which track, and
    later remove it if the track disappears from the playlist. Spotify IDs
    are a fixed 22-char base62 string, so this is safe to parse back out."""
    staging = STAGING_DIR / card_name
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    track_url = f"https://open.spotify.com/track/{track['id']}"
    wrapper = Path(__file__).resolve().parent / "run_spotify_dl.py"
    result = subprocess.run(
        [sys.executable, str(wrapper), "-l", track_url, "-o", str(staging)],
        capture_output=True,
        text=True,
        timeout=180,
        # spotify_dl writes download_list.log into the cwd and then os.rename()s
        # it into the output dir. Under Docker the cwd is on the overlay fs and
        # data/ is a bind mount, so that rename fails with EXDEV. Running from
        # the staging dir keeps both ends on the same filesystem.
        cwd=str(staging),
    )
    if result.returncode != 0:
        raise RuntimeError(f"spotify_dl exited {result.returncode}: {result.stderr.strip()}")

    mp3s = list(staging.rglob("*.mp3"))
    if not mp3s:
        raise RuntimeError("spotify_dl reported success but produced no .mp3")

    dest_dir = WATCH_DIR / card_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{track['id']}--{mp3s[0].name}"
    # Move into the (unwatched-until-complete) staging -> watch. Same filesystem
    # (both under data/), so rename is atomic; the pipeline sees a whole file.
    mp3s[0].replace(dest)
    shutil.rmtree(staging, ignore_errors=True)
    return dest.name


def write_manifest(card_name: str, live_track_ids) -> None:
    """Write the set of currently-live track IDs for this playlist, which the
    Yoto pipeline reads to know which of its own chapters to remove."""
    manifest_path = WATCH_DIR / card_name / ".spotify_track_ids.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(sorted(live_track_ids), f)
    tmp.replace(manifest_path)


def build_client() -> spotipy.Spotify:
    if not Path(TOKEN_CACHE).exists():
        raise RuntimeError(
            f"No Spotify token cache at {TOKEN_CACHE}. Run spotify_auth_setup.py "
            "on a machine with a browser and copy the file here."
        )
    auth = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=CacheFileHandler(cache_path=TOKEN_CACHE),
        open_browser=False,
    )
    return spotipy.Spotify(auth_manager=auth)


def main() -> int:
    if not (SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET):
        log.error("Missing SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET")
        return 1

    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    try:
        sp = build_client()
    except Exception:
        log.exception("Spotify auth unavailable")
        return 1

    try:
        playlists = discover_playlists(sp)
    except Exception:
        log.exception("Failed to list playlists (is the token cache valid?)")
        return 1

    log.info(
        f"{len(playlists)} playlist(s) matching prefix {PLAYLIST_PREFIX!r}: "
        f"{[p['name'] for p in playlists]}"
    )

    for pl in playlists:
        card_name = sanitize(pl["name"][len(PLAYLIST_PREFIX):])
        pstate = state["playlists"].setdefault(
            pl["id"], {"name": pl["name"], "card_name": card_name, "downloaded": {}}
        )
        pstate["name"] = pl["name"]
        pstate["card_name"] = card_name
        pstate.setdefault("downloaded", {})

        try:
            tracks = fetch_playlist_tracks(sp, pl["id"])
        except Exception:
            log.exception(f"Failed to fetch tracks for {pl['name']!r}; skipping")
            continue

        live_ids = {t["id"] for t in tracks}
        write_manifest(card_name, live_ids)

        # Forget any track we'd previously downloaded that's no longer in the
        # playlist, so re-adding it later is treated as new again. The Yoto
        # pipeline is what actually removes the chapter (it reads the manifest
        # above); this just keeps our own dedup bookkeeping in sync.
        removed_ids = [tid for tid in pstate["downloaded"] if tid not in live_ids]
        for tid in removed_ids:
            removed = pstate["downloaded"].pop(tid)
            log.info(f"  No longer in playlist: {removed.get('title')} ({tid})")
        if removed_ids:
            save_state(state)

        new_tracks = [t for t in tracks if t["id"] not in pstate["downloaded"]]
        log.info(
            f"[{pl['name']}] -> card {card_name!r}: "
            f"{len(tracks)} tracks, {len(new_tracks)} new, {len(removed_ids)} removed"
        )

        for track in new_tracks:
            log.info(f"  New: {track['artist']} - {track['title']} ({track['id']})")
            try:
                filename = download_track(track, card_name)
            except Exception:
                log.exception(f"  Failed to download {track['id']}, will retry next run")
                continue

            pstate["downloaded"][track["id"]] = {
                "title": track["title"],
                "artist": track["artist"],
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "filename": filename,
            }
            save_state(state)
            log.info(f"  Downloaded -> {card_name}/{filename}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

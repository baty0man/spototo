"""
Watches data/watch/ (recursively) for new MP3s. Each MP3 lives in a
per-playlist subfolder (data/watch/<card_name>/...) written by the Spotify
sync; the subfolder name is the Yoto MYO card it belongs to. For each file:
read ID3 tags, generate a 16x16 pixel-art icon via Replicate, upload the
audio + icon to Yoto, and append a chapter to that card (creating it on the
first track). An MP3 dropped directly in data/watch/ falls back to the
default card named Config.YOTO_CARD_TITLE. Processed files are moved to
data/processed/, mirroring the watch/ layout.

Files are processed one at a time on a single worker thread, so writes to
different cards never race. The playlist-name -> card-id map is persisted in
state.json under "cards".
"""
import json
import logging
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from mutagen import File as MutagenFile
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import Config
from icon_resolver import IconResolver
from runtime_lock import pipeline_lock
from state import State
from yoto_client import YotoClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("yoto-pixel-pipeline")

# How often to rescan watch/ as a safety net for any filesystem events the
# watcher missed (e.g. a file moved into a brand-new subfolder). Deletion
# reconciliation (see reconcile_deletions) piggybacks on the same tick — cheap
# no-ops until spotify_sync's manifest actually changes, which is every 5 min.
RESCAN_SECONDS = 30

# spotify_sync tags downloaded files as "<22-char-spotify-id>--<name>.mp3" so
# we can map a chapter back to the Spotify track that created it.
TRACK_ID_RE = re.compile(r"^([A-Za-z0-9]{22})--(.+)$")

RECONCILE = object()  # work-queue sentinel meaning "check for deletions"


def wait_for_stable_file(path: Path, stable_seconds: float, poll_interval: float = 0.5):
    last_size = -1
    stable_since = None
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(poll_interval)
            continue
        now = time.time()
        if size == last_size and size > 0:
            if now - stable_since >= stable_seconds:
                return
        else:
            last_size = size
            stable_since = now
        time.sleep(poll_interval)


def read_tags(path: Path):
    title, artist = None, None
    try:
        tags = MutagenFile(path, easy=True)
        if tags:
            title = (tags.get("title") or [None])[0]
            artist = (tags.get("artist") or [None])[0]
    except Exception:
        log.exception(f"Failed to read tags from {path.name}, falling back to filename")
    if not title:
        title = path.stem
    return title, artist


def parse_track_id(filename: str):
    """(spotify_track_id, original_filename) if spotify_sync tagged this file,
    else (None, filename) — e.g. for an MP3 dropped in manually."""
    m = TRACK_ID_RE.match(filename)
    if m:
        return m.group(1), m.group(2)
    return None, filename


def target_card_name(path: Path) -> str:
    """The card a file belongs to: its immediate parent folder name, or the
    default card for files dropped directly into WATCH_DIR."""
    parent = path.parent.resolve()
    if parent == Config.WATCH_DIR.resolve():
        return Config.YOTO_CARD_TITLE
    return parent.name


def resolve_card_id(state: State, card_name: str):
    """Existing Yoto card id for this name, or None if it must be created."""
    cards = state.get("cards", {}) or {}
    card_id = cards.get(card_name)
    if card_id is None and card_name == Config.YOTO_CARD_TITLE and Config.YOTO_CARD_ID:
        card_id = Config.YOTO_CARD_ID  # honor the legacy single-card config
    return card_id


def remember_card(state: State, card_name: str, card_id: str):
    cards = state.get("cards", {}) or {}
    if cards.get(card_name) != card_id:
        cards[card_name] = card_id
        state.set("cards", cards)


def next_chapter_num(existing_chapters) -> int:
    """max(existing chapter key) + 1, not len(existing) + 1 — chapter keys can
    have gaps once reconcile_deletions starts removing chapters, and reusing
    count-based numbering can collide with an already-used key (this bit us
    once already: two chapters ended up sharing the same key)."""
    used = []
    for ch in existing_chapters:
        try:
            used.append(int(ch.get("key", "")))
        except (TypeError, ValueError):
            continue
    return max(used, default=0) + 1


def resolve_icon(client: YotoClient, title: str, icon_filename_stem: str):
    """Resolve an optional icon, reusing one resolver for this pipeline client."""
    resolver = vars(client).get("_spototo_icon_resolver")
    if resolver is None:
        resolver = IconResolver(client, Config)
        client._spototo_icon_resolver = resolver
    return resolver.resolve(title, icon_filename_stem)

def process_file(path: Path, client: YotoClient, state: State):
    card_name = target_card_name(path)
    track_id, original_name = parse_track_id(path.name)
    log.info(f"New file: {path.name} (card {card_name!r})")
    wait_for_stable_file(path, Config.STABLE_WAIT_SECONDS)

    title, artist = read_tags(path)
    log.info(f"Tags -> title={title} artist={artist}")

    media_id = resolve_icon(client, title, Path(original_name).stem)

    audio_info = client.upload_audio(str(path))
    log.info(f"Audio transcoded, sha256={audio_info['sha256']}")

    card_id = resolve_card_id(state, card_name)
    card_id, existing_chapters = client.get_card_and_chapters(card_id)
    chapter_num = next_chapter_num(existing_chapters)
    chapter = client.build_chapter(chapter_num, title, artist, audio_info, media_id)
    new_card_id = client.save_chapters(
        card_id, existing_chapters + [chapter], title=card_name
    )
    remember_card(state, card_name, new_card_id)

    if track_id:
        # Record which chapter this Spotify track became, so reconcile_deletions
        # can remove it later if the track disappears from the playlist. Only
        # spotify-sourced files (tagged with an id) are ever tracked/removed
        # this way — chapters added any other way are never touched.
        chapters_map = state.get("chapters", {}) or {}
        chapters_map.setdefault(card_name, {})[track_id] = f"{chapter_num:02d}"
        state.set("chapters", chapters_map)

    # Mirror the watch/ subfolder layout under processed/, using the original
    # (track-id-stripped) filename for readability.
    try:
        rel = path.resolve().relative_to(Config.WATCH_DIR.resolve())
    except ValueError:
        rel = Path(path.name)
    processed_path = Config.PROCESSED_DIR / rel.parent / original_name
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(processed_path))

    log.info(f"Done: {rel} -> chapter {chapter_num:02d} on card {new_card_id} ({card_name!r})")


def reconcile_deletions(client: YotoClient, state: State):
    """Remove chapters whose Spotify track is no longer in its playlist.

    Only ever touches chapters this pipeline itself recorded in state.json's
    "chapters" map (see process_file) — a card's pre-existing or manually
    added chapters have no entry there and are never considered for removal.
    Compares against the manifest spotify_sync writes to
    data/watch/<card_name>/.spotify_track_ids.json; a missing manifest (e.g.
    the playlist was renamed off the Yoto- prefix) means "don't know", not
    "everything's gone", so that card is skipped rather than emptied.
    """
    chapters_map = state.get("chapters", {}) or {}
    for card_name, track_map in list(chapters_map.items()):
        manifest_path = Config.WATCH_DIR / card_name / ".spotify_track_ids.json"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path) as f:
                live_ids = set(json.load(f))
        except (json.JSONDecodeError, OSError):
            log.warning(f"[{card_name}] couldn't read manifest; skipping reconcile")
            continue

        stale_ids = [tid for tid in track_map if tid not in live_ids]
        if not stale_ids:
            continue

        card_id = resolve_card_id(state, card_name)
        if not card_id:
            continue

        card_id, existing_chapters = client.get_card_and_chapters(card_id)
        stale_keys = {track_map[tid] for tid in stale_ids}
        remaining = [ch for ch in existing_chapters if ch.get("key") not in stale_keys]
        if len(remaining) != len(existing_chapters):
            client.save_chapters(card_id, remaining, title=card_name)
            log.info(
                f"[{card_name}] removed {len(existing_chapters) - len(remaining)} "
                f"chapter(s) no longer in the playlist"
            )

        for tid in stale_ids:
            del track_map[tid]
        if not track_map:
            del chapters_map[card_name]

    state.set("chapters", chapters_map)


class MP3Handler(FileSystemEventHandler):
    def __init__(self, enqueue):
        self.enqueue = enqueue

    def on_created(self, event):
        if not event.is_directory:
            self.enqueue(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.enqueue(event.dest_path)


def main():
    Config.validate()
    with pipeline_lock(Config.STATE_FILE):
        _run_pipeline()


def _run_pipeline():
    state = State(Config.STATE_FILE)
    client = YotoClient(Config, state)

    work_queue = queue.Queue()
    enqueued = set()
    reconcile_pending = False
    enq_lock = threading.Lock()

    def enqueue(src_path):
        p = Path(src_path)
        if p.suffix.lower() != ".mp3":
            return
        p = p.resolve()
        with enq_lock:
            if p in enqueued:
                return
            enqueued.add(p)
        work_queue.put(p)

    def enqueue_reconcile():
        nonlocal reconcile_pending
        with enq_lock:
            if reconcile_pending:
                return
            reconcile_pending = True
        work_queue.put(RECONCILE)

    def worker():
        nonlocal reconcile_pending
        while True:
            item = work_queue.get()
            try:
                if item is RECONCILE:
                    reconcile_deletions(client, state)
                elif item.exists():
                    process_file(item, client, state)
            except Exception:
                log.exception(f"Failed to process queue item {item!r}; will retry")
            finally:
                with enq_lock:
                    if item is RECONCILE:
                        reconcile_pending = False
                    else:
                        enqueued.discard(item)
                work_queue.task_done()

    threading.Thread(target=worker, daemon=True).start()

    observer = Observer()
    observer.schedule(MP3Handler(enqueue), str(Config.WATCH_DIR), recursive=True)
    observer.start()
    log.info(f"Watching {Config.WATCH_DIR} (recursive) for new MP3s...")

    try:
        while True:
            # Startup + periodic safety-net rescan (dedup handled by enqueue()).
            for existing in sorted(Config.WATCH_DIR.rglob("*.mp3")):
                enqueue(existing)
            enqueue_reconcile()
            time.sleep(RESCAN_SECONDS)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()

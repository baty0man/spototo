"""Library-first icons with conservative persistent paid-attempt accounting."""
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import unicodedata

from config import Config
from icon_library import find_library_icon
from runtime_lock import pipeline_lock

log = logging.getLogger("yoto-pixel-pipeline.icons")
PROMPT_STYLE = "cute simple flat pixel art icon, single centered subject, bold outline"
MODES = {"generate", "yoto-library", "yoto-library-ai", "none"}


def validate_icon_config(config):
    if config.ICON_SOURCE not in MODES:
        raise ValueError("Invalid ICON_SOURCE")
    cap = getattr(config, "ICON_AI_MAX_GENERATIONS", 0)
    if type(cap) is not int or cap < 0:
        raise ValueError("ICON_AI_MAX_GENERATIONS must be a nonnegative integer")


def _atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=".icon-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class IconResolver:
    def __init__(self, client, config=Config):
        validate_icon_config(config)
        self.client, self.config = client, config

    def resolve(self, title, filename_stem):
        mode = self.config.ICON_SOURCE
        if mode == "none":
            return None
        if mode in {"yoto-library", "yoto-library-ai"}:
            try:
                media_id = find_library_icon(self.client, title)
            except Exception:
                log.warning("Icon library unavailable; leaving icon unset")
                return None  # an outage is not a no-match, never paid fallback
            if media_id or mode == "yoto-library":
                return media_id
        try:
            return self._generated(title, filename_stem, guarded=mode == "yoto-library-ai")
        except Exception:
            log.warning("Icon resolution failed; leaving icon unset")
            return None

    def _generated(self, title, filename_stem, guarded):
        # Import only on the generation/cache path; library mode needs no SDK.
        import art_generator as art
        normalized = " ".join(unicodedata.normalize("NFKC", title).casefold().split())
        settings = {name: getattr(art, name) for name in
                    ("MODEL", "STYLE", "ICON_SIZE", "NUM_CANDIDATES", "SUBJECT_MODEL", "SUBJECT_SYSTEM", "JUDGE_MODEL")}
        key = hashlib.sha256(json.dumps({"title": normalized, "settings": settings,
            "prompt_style": PROMPT_STYLE, "version": 1}, sort_keys=True).encode()).hexdigest()
        cache = Path(getattr(self.config, "ICON_CACHE_DIR", Config.ICON_CACHE_DIR)).absolute()
        sync_directories = [cache, *cache.parents]
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cache.is_symlink():
            raise ValueError("Unsafe icon cache")
        cache.chmod(0o700)
        # Existing directories may survive a failed fsync or process restart.
        # Persist the full chain through root on every attempt before reserving
        # budget or starting paid work; existence is not evidence of durability.
        for directory in sync_directories:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        ledger_path = cache / "cache.json"
        png_path = cache / (key + ".png")
        with pipeline_lock(ledger_path):
            if ledger_path.exists():
                ledger = json.loads(ledger_path.read_text())
                if (type(ledger.get("attempts")) is not int or ledger["attempts"] < 0
                        or not isinstance(ledger.get("entries"), dict)
                        or ledger["attempts"] < len(ledger["entries"])):
                    raise ValueError("Invalid icon cache ledger")
                for entry in ledger["entries"].values():
                    if (not isinstance(entry, dict)
                            or entry.get("status") not in ("reserved", "uploaded")
                            or (entry["status"] == "uploaded" and
                                (not isinstance(entry.get("media_id"), str) or not entry["media_id"]))):
                        raise ValueError("Invalid icon cache entry")
            else:
                # Missing ledger alongside cached artifacts is uncertain: fail closed.
                if any(cache.glob("*.png")):
                    raise ValueError("Missing icon cache ledger")
                ledger = {"attempts": 0, "entries": {}}
            entries = ledger["entries"]
            if key in entries:
                entry = entries[key]
                if entry["status"] == "uploaded":
                    return entry["media_id"]
                if not png_path.exists():
                    return None  # reserved/failed/uncertain attempt: no automatic paid retry
            else:
                token = self.config.REPLICATE_API_TOKEN
                cap = getattr(self.config, "ICON_AI_MAX_GENERATIONS", 0)
                if not token or (guarded and (cap == 0 or ledger["attempts"] >= cap)):
                    return None
                ledger["attempts"] += 1
                entries[key] = {"status": "reserved"}
                _atomic_write(ledger_path, json.dumps(ledger, sort_keys=True).encode())
                # The legacy generator logs raw judge exceptions. Suppress that logger
                # inside the serialized paid step so provider credentials cannot leak.
                old_disabled = art.log.disabled
                art.log.disabled = True
                try:
                    subject = art.extract_subject(title, token)
                    png = art.generate_pixel_art(f"{subject}, {PROMPT_STYLE}", token, subject=subject)
                finally:
                    art.log.disabled = old_disabled
                _atomic_write(png_path, png)  # durable before the first upload
            media_id = self.client.upload_icon(png_path.read_bytes(), filename=f"{Path(filename_stem).name}.png")
            if not isinstance(media_id, str) or not media_id:
                raise ValueError("Invalid uploaded icon ID")
            entries[key] = {"status": "uploaded", "media_id": media_id}
            _atomic_write(ledger_path, json.dumps(ledger, sort_keys=True).encode())
            return media_id

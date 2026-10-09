"""
Deterministic, non-AI icon selection: matches a song title against Yoto's
own official icon library by keyword/tag overlap. No LLM, no image
generation -- this is the alternative to art_generator.py for anyone who
doesn't want any AI involved in icon selection (Config.ICON_SOURCE = "yoto-library").

If nothing overlaps well enough to count as a real match, returns None
rather than guessing -- callers should leave the chapter without a custom
icon in that case, not fall back to generation.
"""
import re

_STOPWORDS = {
    "the", "a", "an", "of", "by", "and", "or", "is", "in", "on", "to", "for",
    "with", "feat", "featuring", "remix", "version", "from", "original",
    "soundtrack",
}

_library_cache = None  # [(mediaId, {tokens}), ...] -- fetched once per process


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _load_library(client):
    global _library_cache
    if _library_cache is None:
        icons = client.list_library_icons()
        if not isinstance(icons, list):
            raise ValueError("Icon library must be a list")
        # Validate the entire response before caching or treating it as no-match.
        for icon in icons:
            if not isinstance(icon, dict):
                raise ValueError("Icon library record must be an object")
            media_id = icon.get("mediaId")
            if not isinstance(media_id, str) or not media_id.strip():
                raise ValueError("Icon library mediaId must be a nonempty string")
            if not isinstance(icon.get("title", ""), str):
                raise ValueError("Icon library title must be a string")
            tags = icon.get("publicTags", [])
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                raise ValueError("Icon library publicTags must be a list of strings")
        _library_cache = [
            (
                icon["mediaId"],
                _tokens(icon.get("title", "")) | {t.lower() for t in icon.get("publicTags", [])},
            )
            for icon in icons
        ]
    return _library_cache


def find_library_icon(client, title: str):
    """Returns the mediaId of the best-matching official Yoto icon for
    `title`, or None if no icon shares even one meaningful keyword/tag."""
    library = _load_library(client)
    query = _tokens(title)
    if not query:
        return None
    best_id, best_score = None, 0
    for media_id, tags in library:
        score = len(query & tags)
        if score > best_score:
            best_id, best_score = media_id, score
    return best_id if best_score > 0 else None

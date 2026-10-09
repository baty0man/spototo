"""Preview or safely backfill missing icons on an existing MYO card.

Run ``python src/backfill_icons.py --card-id ID``; add ``--apply`` to write.
No audio is uploaded. The CLI holds the pipeline's shared state lock even
in preview mode, because reading a card may rotate OAuth refresh tokens.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import uuid


class BackfillError(RuntimeError):
    """A safe, credential-free backfill failure message."""


def _card(raw):
    if not isinstance(raw, dict):
        raise BackfillError('Card not found or invalid response.')
    card = raw.get('card', raw)
    if not isinstance(card, dict) or card.get('deleted'):
        raise BackfillError('Card not found or deleted.')
    content = card.get('content')
    if not isinstance(card.get('title'), str) or not isinstance(content, dict):
        raise BackfillError('Card title or content is invalid; no changes saved.')
    chapters = content.get('chapters')
    if not isinstance(chapters, list):
        raise BackfillError('Card chapters are invalid; no changes saved.')
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise BackfillError('Card chapter is invalid; no changes saved.')
        tracks = chapter.get('tracks', [])
        if not isinstance(tracks, list) or any(not isinstance(t, dict) for t in tracks):
            raise BackfillError('Card tracks are invalid; no changes saved.')
        for item in [chapter, *tracks]:
            if item.get('display') is not None and not isinstance(item['display'], dict):
                raise BackfillError('Card display is invalid; no changes saved.')
    return card


def _icon(item):
    return (item.get('display') or {}).get('icon16x16')


def _fill(item, icon):
    if not icon or _icon(item):
        return 0
    if item.get('display') is None:
        item['display'] = {}
    item['display']['icon16x16'] = icon
    return 1


def _resolve(resolver, title):
    if resolver is None or not title:
        return None
    try:
        media_id = resolver.resolve(title, title)
        return f'yoto:#{media_id}' if isinstance(media_id, str) and media_id else None
    except Exception:
        # Icon failure must not affect playable audio or leak provider responses.
        return None


def _backup(raw, backup_dir):
    directory = Path(backup_dir).absolute()
    sync_directories = [directory, *directory.parents]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError('Invalid backup directory')
    os.chmod(directory, 0o700)
    # No card ID/title in the filename: neither is trusted as a filesystem path.
    path = directory / f'card-{uuid.uuid4().hex}.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(raw, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    # Persist the snapshot and every ancestor entry, even on a retry: existing
    # directories may remain after an earlier fsync failure without being durable.
    for directory in sync_directories:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return path


def backfill(client, card_id, *, apply=False, resolver=None,
             backup_dir='data/icon-backfill-backups'):
    """Backfill a card using injected clients; callers must hold the shared lock.

    Preview never invokes the resolver. Only successful readback permits an
    ``applied`` result. The original raw GET response is backed up before POST.
    """
    try:
        original = deepcopy(client.get_content(card_id))
        card = _card(original)
    except BackfillError:
        raise
    except Exception:
        raise BackfillError('Could not read card; no changes saved.') from None
    updated = deepcopy(card['content'])
    missing = []
    for ci, chapter in enumerate(updated['chapters']):
        if not _icon(chapter):
            missing.append({'chapter': ci, 'key': chapter.get('key'), 'track': None})
        for ti, track in enumerate(chapter.get('tracks', [])):
            if not _icon(track):
                missing.append({'chapter': ci, 'key': chapter.get('key'),
                                'track': ti, 'track_key': track.get('key')})
    result = {'missing': len(missing), 'missing_icons': missing,
              'changed': 0, 'applied': False, 'backup': None}
    if not apply or not missing:
        return result

    for chapter in updated['chapters']:
        tracks = chapter.get('tracks', [])
        title = (chapter.get('title') or '').split(' — ', 1)[0].strip()
        # Reuse an existing track icon before attempting any resolution.
        chosen = next((_icon(t) for t in tracks if _icon(t)), None)
        if len(tracks) == 1:
            chosen = chosen or _icon(chapter)
            if not chosen:
                chosen = _resolve(resolver, tracks[0].get('title') or title)
            result['changed'] += _fill(tracks[0], chosen)
        elif tracks:
            for track in tracks:
                if not _icon(track):
                    icon = _resolve(resolver, track.get('title') or title)
                    result['changed'] += _fill(track, icon)
                    chosen = chosen or icon
        elif not _icon(chapter):
            chosen = _resolve(resolver, title)
        result['changed'] += _fill(chapter, chosen)

    if not result['changed']:
        return result
    try:
        backup = _backup(original, backup_dir)
    except Exception:
        raise BackfillError('Backup failed; no card changes saved.') from None
    try:
        latest = _card(client.get_content(card_id))
        if latest['content'] != card['content'] or latest['title'] != card['title']:
            raise BackfillError('Card changed during backfill; no changes saved.')
        client._post_json('/content', {'cardId': card_id, 'title': card['title'],
                                      'content': updated})
        readback = _card(client.get_content(card_id))
        if (readback['content'] != updated or readback['title'] != card['title']):
            raise BackfillError('Saved card could not be verified; retain the backup for recovery.')
    except BackfillError:
        raise
    except Exception:
        raise BackfillError('Card save or verification failed; retain the backup for recovery.') from None
    result.update(applied=True, backup=str(backup))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--card-id', required=True)
    parser.add_argument('--apply', action='store_true', help='Resolve icons and save (default: preview)')
    parser.add_argument('--backup-dir', default='data/icon-backfill-backups')
    args = parser.parse_args(argv)
    try:
        # Lazy imports allow --help and preview without loading AI dependencies.
        from config import Config
        from runtime_lock import pipeline_lock
        from state import State
        from yoto_client import YotoClient
        with pipeline_lock(Config.STATE_FILE):
            state = State(Config.STATE_FILE)
            client = YotoClient(Config, state)
            resolver = None
            if args.apply:
                from icon_resolver import IconResolver
                # Explicit hybrid policy; generation allowance/token checks stay
                # in the shared resolver rather than duplicating paid-call logic.
                class BackfillConfig(Config):
                    ICON_SOURCE = 'yoto-library-ai'
                resolver = IconResolver(client, config=BackfillConfig)
            result = backfill(client, args.card_id, apply=args.apply, resolver=resolver,
                              backup_dir=args.backup_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except BackfillError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print('Backfill failed; check configuration, authentication, and pipeline lock. '
              'No verified success was recorded.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

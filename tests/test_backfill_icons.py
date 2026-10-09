import copy
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
from typing import Any
import unittest
from contextlib import contextmanager
from unittest.mock import patch


def snapshot():
    return {'card': {'cardId': 'card-1', 'title': 'Original', 'content': {
        'unknown': {'keep': [1, 2]}, 'chapters': [
            {'key': '07', 'title': 'Song — Artist', 'display': None,
             'custom': 'keep', 'tracks': [{'key': '09', 'title': 'Track Song',
             'trackUrl': 'yoto:#audio', 'duration': 12.5, 'fileSize': 456,
             'display': {'colour': 'red'}, 'other': {'x': True}}]}
        ]}, 'metadata': {'untouched': True}}}


class Client:
    def __init__(self, raw=None):
        self.raw = copy.deepcopy(raw or snapshot())
        self.calls = []
        self.conflict: Any = None
        self.bad_readback = False

    def get_content(self, card_id):
        self.calls.append(('get', card_id))
        if self.conflict and len([c for c in self.calls if c[0] == 'get']) == 2:
            self.conflict(self.raw)
        result = copy.deepcopy(self.raw)
        if self.bad_readback and any(c[0] == 'post' for c in self.calls):
            result['card']['content']['chapters'][0]['tracks'][0]['duration'] = 999
        return result

    def _post_json(self, path, payload):
        self.calls.append(('post', path, copy.deepcopy(payload)))
        self.raw['card']['content'] = copy.deepcopy(payload['content'])
        self.raw['card']['title'] = payload['title']
        return {'cardId': 'card-1'}

    def upload_audio(self, *args):
        raise AssertionError('Audio must never be uploaded')


class Resolver:
    def __init__(self, value='icon-id'):
        self.value: Any = value
        self.calls = []

    def resolve(self, title, filename_stem):
        self.calls.append((title, filename_stem))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class BackfillTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('backfill_icons')
        except ModuleNotFoundError:
            self.fail('safe icon-only backfill implementation is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = Client()
        self.resolver: Any = Resolver()

    def run_backfill(self, **kwargs):
        return self.module.backfill(self.client, 'card-1', resolver=self.resolver,
                                    backup_dir=Path(self.temp.name) / 'backups', **kwargs)

    def test_preview_never_resolves_uploads_or_writes(self):
        result = self.run_backfill()
        self.assertEqual(result['missing'], 2)
        self.assertFalse(result['applied'])
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.client.calls, [('get', 'card-1')])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_apply_preserves_every_non_icon_field_and_backs_up(self):
        before = copy.deepcopy(self.client.raw)
        result = self.run_backfill(apply=True)
        self.assertTrue(result['applied'])
        self.assertEqual(result['changed'], 2)
        self.assertEqual(self.resolver.calls[0][0], 'Track Song')
        chapter = self.client.raw['card']['content']['chapters'][0]
        self.assertEqual(chapter['display']['icon16x16'], 'yoto:#icon-id')
        self.assertEqual(chapter['tracks'][0]['display']['icon16x16'], 'yoto:#icon-id')
        del chapter['display']['icon16x16']
        chapter['display'] = None
        del chapter['tracks'][0]['display']['icon16x16']
        self.assertEqual(self.client.raw, before)
        backup = Path(result['backup'])
        self.assertEqual(json.loads(backup.read_text()), before)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        posts = [c for c in self.client.calls if c[0] == 'post']
        self.assertEqual(posts[0][1], '/content')
        self.assertEqual(posts[0][2]['cardId'], 'card-1')
        self.assertEqual(posts[0][2]['title'], 'Original')

    def test_existing_track_icon_reused_without_resolution(self):
        track = self.client.raw['card']['content']['chapters'][0]['tracks'][0]
        track['display']['icon16x16'] = 'yoto:#existing'
        self.run_backfill(apply=True)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.client.raw['card']['content']['chapters'][0]['display'],
                         {'icon16x16': 'yoto:#existing'})
        self.assertEqual(track['display']['icon16x16'], 'yoto:#existing')

    def test_existing_chapter_icon_reused_for_single_track(self):
        self.client.raw['card']['content']['chapters'][0]['display'] = {'icon16x16': 'yoto:#old'}
        self.run_backfill(apply=True)
        self.assertEqual(self.resolver.calls, [])
        chapter = self.client.raw['card']['content']['chapters'][0]
        self.assertEqual(chapter['tracks'][0]['display']['icon16x16'], 'yoto:#old')

    def test_multitrack_resolves_individually_without_overwriting(self):
        chapter = self.client.raw['card']['content']['chapters'][0]
        chapter['display'] = {'icon16x16': 'yoto:#chapter', 'custom': 1}
        chapter['tracks'].extend([
            {'key': '10', 'title': 'Second', 'trackUrl': 'url2', 'display': None},
            {'key': '11', 'title': 'Third', 'trackUrl': 'url3',
             'display': {'icon16x16': 'yoto:#third'}}])
        self.run_backfill(apply=True)
        self.assertEqual([call[0] for call in self.resolver.calls], ['Track Song', 'Second'])
        saved = self.client.raw['card']['content']['chapters'][0]
        self.assertEqual(saved['display'], {'icon16x16': 'yoto:#chapter', 'custom': 1})
        self.assertEqual(saved['tracks'][2]['display']['icon16x16'], 'yoto:#third')
        self.assertEqual(saved['tracks'][1]['trackUrl'], 'url2')

    def test_missing_track_title_uses_chapter_without_artist(self):
        del self.client.raw['card']['content']['chapters'][0]['tracks'][0]['title']
        self.run_backfill(apply=True)
        self.assertEqual(self.resolver.calls[0][0], 'Song')

    def test_null_track_display_supported(self):
        self.client.raw['card']['content']['chapters'][0]['tracks'][0]['display'] = None
        self.run_backfill(apply=True)
        self.assertTrue(self.client.raw['card']['content']['chapters'][0]['tracks'][0]['display']['icon16x16'])

    def test_retry_is_idempotent(self):
        self.run_backfill(apply=True)
        first_calls = len(self.resolver.calls)
        second = self.run_backfill(apply=True)
        self.assertEqual(second['changed'], 0)
        self.assertFalse(second['applied'])
        self.assertEqual(len(self.resolver.calls), first_calls)
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'post']), 1)

    def test_no_resolution_means_no_backup_or_write(self):
        self.resolver.value = None
        result = self.run_backfill(apply=True)
        self.assertFalse(result['applied'])
        self.assertFalse(any(c[0] == 'post' for c in self.client.calls))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_resolver_failure_keeps_playable_content(self):
        before = copy.deepcopy(self.client.raw)
        self.resolver.value = RuntimeError('SECRET credentials')
        result = self.run_backfill(apply=True)
        self.assertFalse(result['applied'])
        self.assertEqual(self.client.raw, before)
        self.assertNotIn('SECRET', str(result))

    def test_concurrent_content_change_aborts(self):
        self.client.conflict = lambda raw: raw['card']['content'].update(unknown='edited')
        with self.assertRaises(self.module.BackfillError):
            self.run_backfill(apply=True)
        self.assertFalse(any(c[0] == 'post' for c in self.client.calls))

    def test_concurrent_title_change_aborts(self):
        self.client.conflict = lambda raw: raw['card'].update(title='Edited')
        with self.assertRaises(self.module.BackfillError):
            self.run_backfill(apply=True)
        self.assertFalse(any(c[0] == 'post' for c in self.client.calls))

    def test_backup_failure_prevents_card_write(self):
        with patch.object(self.module, '_backup', side_effect=OSError('SECRET')):
            with self.assertRaises(self.module.BackfillError) as caught:
                self.run_backfill(apply=True)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertFalse(any(c[0] == 'post' for c in self.client.calls))

    def test_backup_fsyncs_file_and_directory_chain_before_card_write(self):
        fsync = os.fsync
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / 'parent' / 'backups'
                if existing:
                    directory.mkdir(parents=True)
                self.client = Client()
                synced = []
                def sync(fd):
                    self.assertEqual(self.client.calls, [('get', 'card-1')])
                    synced.append(os.fstat(fd))
                    fsync(fd)
                with patch.object(self.module.os, 'fsync', side_effect=sync):
                    result = self.module.backfill(self.client, 'card-1', apply=True,
                        resolver=self.resolver, backup_dir=directory)
                self.assertTrue(result['applied'])
                self.assertTrue(stat.S_ISREG(synced[0].st_mode))
                expected = [directory, *directory.parents]
                self.assertEqual(
                    [(s.st_dev, s.st_ino) for s in synced[1:]],
                    [(p.stat().st_dev, p.stat().st_ino) for p in expected])
                self.assertTrue(all(stat.S_ISDIR(s.st_mode) for s in synced[1:]))

    def test_backup_directory_fsync_failure_prevents_card_write(self):
        fsync = os.fsync
        for failed_level in ('backups', 'parent', 'root'):
            with self.subTest(failed_level=failed_level), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / 'parent' / 'backups'
                failed = {'backups': directory, 'parent': directory.parent,
                          'root': Path(root)}[failed_level]
                self.client = Client()
                before = copy.deepcopy(self.client.raw)
                def sync(fd):
                    info = os.fstat(fd)
                    if stat.S_ISDIR(info.st_mode) and info.st_ino == failed.stat().st_ino:
                        raise OSError('SECRET directory fsync failure')
                    fsync(fd)
                with patch.object(self.module.os, 'fsync', side_effect=sync):
                    with self.assertRaisesRegex(self.module.BackfillError, r'^Backup failed; no card changes saved\.$'):
                        self.module.backfill(self.client, 'card-1', apply=True,
                            resolver=self.resolver, backup_dir=directory)
                self.assertEqual(self.client.calls, [('get', 'card-1')])
                self.assertEqual(self.client.raw, before)

    def test_backup_retry_resyncs_ancestors_before_card_write(self):
        fsync = os.fsync
        for failed_level in ('parent', 'root'):
            with self.subTest(failed_level=failed_level), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / 'parent' / 'backups'
                failed = directory.parent if failed_level == 'parent' else Path(root)
                self.client = Client()
                before = copy.deepcopy(self.client.raw)
                synced = []
                fail_sync = True

                def sync(fd):
                    self.assertFalse(any(c[0] == 'post' for c in self.client.calls))
                    info = os.fstat(fd)
                    synced.append(info)
                    if (fail_sync and stat.S_ISDIR(info.st_mode)
                            and (info.st_dev, info.st_ino)
                            == (failed.stat().st_dev, failed.stat().st_ino)):
                        raise OSError('SECRET parent fsync failure')
                    fsync(fd)

                post_json = self.client._post_json

                def assert_synced_before_post(path, payload):
                    self.assertTrue(stat.S_ISREG(synced[0].st_mode))
                    expected = [directory, *directory.parents]
                    self.assertEqual(
                        [(s.st_dev, s.st_ino) for s in synced[1:]],
                        [(p.stat().st_dev, p.stat().st_ino) for p in expected])
                    self.assertTrue(all(stat.S_ISDIR(s.st_mode) for s in synced[1:]))
                    return post_json(path, payload)

                with patch.object(self.module.os, 'fsync', side_effect=sync), \
                        patch.object(self.client, '_post_json',
                                     side_effect=assert_synced_before_post) as post:
                    for attempt in range(2):
                        synced.clear()
                        with self.assertRaisesRegex(self.module.BackfillError,
                                r'^Backup failed; no card changes saved\.$'):
                            self.module.backfill(self.client, 'card-1', apply=True,
                                resolver=self.resolver, backup_dir=directory)
                        self.assertTrue(directory.is_dir())
                        self.assertEqual(len(list(directory.glob('card-*.json'))), attempt + 1)
                        self.assertEqual(self.client.raw, before)
                        post.assert_not_called()
                    fail_sync = False
                    synced.clear()
                    result = self.module.backfill(self.client, 'card-1', apply=True,
                        resolver=self.resolver, backup_dir=directory)
                self.assertTrue(result['applied'])
                self.assertEqual(json.loads(Path(result['backup']).read_text()), before)
                post.assert_called_once()

    def test_readback_failure_cannot_report_success(self):
        self.client.bad_readback = True
        with self.assertRaises(self.module.BackfillError):
            self.run_backfill(apply=True)

    def test_invalid_card_shape_fails_without_write(self):
        self.client.raw = {'content': {}}
        with self.assertRaises(self.module.BackfillError):
            self.run_backfill(apply=True)
        self.assertEqual(self.resolver.calls, [])

    def test_cli_preview_locks_before_state_and_never_constructs_resolver(self):
        events = []
        @contextmanager
        def lock(path):
            events.append('lock')
            yield
            events.append('unlock')
        config = types.SimpleNamespace(STATE_FILE=Path(self.temp.name) / 'state.json')
        def state(path):
            self.assertEqual(events, ['lock'])
            events.append('state')
            return object()
        fake_modules = {
            'config': types.SimpleNamespace(Config=config),
            'runtime_lock': types.SimpleNamespace(pipeline_lock=lock),
            'state': types.SimpleNamespace(State=state),
            'yoto_client': types.SimpleNamespace(YotoClient=lambda config, state: self.client),
            'icon_resolver': types.SimpleNamespace(IconResolver=lambda *a, **k: self.fail('preview resolver')),
        }
        with patch.dict(sys.modules, fake_modules), patch('builtins.print'):
            self.assertEqual(self.module.main(['--card-id', 'card-1']), 0)
        self.assertEqual(events, ['lock', 'state', 'unlock'])

    def test_cli_apply_uses_guarded_library_ai_mode(self):
        @contextmanager
        def lock(path):
            yield
        class Configuration:
            STATE_FILE = Path(self.temp.name) / 'state.json'
            ICON_SOURCE = 'generate'
            ICON_AI_MAX_GENERATIONS = 0
            REPLICATE_API_TOKEN = ''
        def resolver(client, config):
            self.assertEqual(config.ICON_SOURCE, 'yoto-library-ai')
            self.assertEqual(config.ICON_AI_MAX_GENERATIONS, 0)
            return self.resolver
        modules = {'config': types.SimpleNamespace(Config=Configuration),
                   'runtime_lock': types.SimpleNamespace(pipeline_lock=lock),
                   'state': types.SimpleNamespace(State=lambda path: object()),
                   'yoto_client': types.SimpleNamespace(YotoClient=lambda *args: self.client),
                   'icon_resolver': types.SimpleNamespace(IconResolver=resolver)}
        with patch.dict(sys.modules, modules), patch('builtins.print'):
            self.assertEqual(self.module.main(['--card-id', 'card-1', '--apply',
                '--backup-dir', str(Path(self.temp.name) / 'backups')]), 0)
        self.assertTrue(any(c[0] == 'post' for c in self.client.calls))

    def test_real_icon_resolver_library_hit_never_generates(self):
        from icon_resolver import IconResolver
        configuration: Any = types.SimpleNamespace(ICON_SOURCE='yoto-library-ai',
                                             ICON_AI_MAX_GENERATIONS=0,
                                             REPLICATE_API_TOKEN='')
        self.resolver = IconResolver(self.client, config=configuration)
        with patch('icon_resolver.find_library_icon', return_value='library-id'), \
             patch.object(self.resolver, '_generated', side_effect=AssertionError('paid generation')) as paid:
            result = self.run_backfill(apply=True)
        self.assertTrue(result['applied'])
        paid.assert_not_called()
        chapter = self.client.raw['card']['content']['chapters'][0]
        self.assertEqual(chapter['display']['icon16x16'], 'yoto:#library-id')

    def test_real_shared_lock_blocks_cli_before_reading_state(self):
        from runtime_lock import pipeline_lock
        state_path = Path(self.temp.name) / 'isolated-state.json'
        modules = {'config': types.SimpleNamespace(Config=types.SimpleNamespace(STATE_FILE=state_path)),
                   'state': types.SimpleNamespace(State=lambda path: self.fail('state touched under active lock')),
                   'yoto_client': types.SimpleNamespace(YotoClient=lambda *args: self.client)}
        with pipeline_lock(state_path), patch.dict(sys.modules, modules), patch('builtins.print'):
            self.assertEqual(self.module.main(['--card-id', 'card-1']), 1)
        self.assertEqual(self.client.calls, [])

    def test_backup_path_is_exclusive(self):
        backup_dir = Path(self.temp.name) / 'backups'
        with patch.object(self.module.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='same')):
            first = self.module._backup(snapshot(), backup_dir)
            with self.assertRaises(FileExistsError):
                self.module._backup({'different': True}, backup_dir)
        self.assertEqual(json.loads(first.read_text()), snapshot())

    def test_cli_masks_raw_failures(self):
        with patch.object(self.module, 'backfill', side_effect=RuntimeError('SECRET')):
            @contextmanager
            def lock(path):
                yield
            modules = {'config': types.SimpleNamespace(Config=types.SimpleNamespace(STATE_FILE='unused')),
                       'runtime_lock': types.SimpleNamespace(pipeline_lock=lock),
                       'state': types.SimpleNamespace(State=lambda path: object()),
                       'yoto_client': types.SimpleNamespace(YotoClient=lambda *args: self.client)}
            with patch.dict(sys.modules, modules), patch('builtins.print') as printer:
                self.assertEqual(self.module.main(['--card-id', 'card-1']), 1)
            self.assertNotIn('SECRET', str(printer.call_args_list))


if __name__ == '__main__':
    unittest.main()

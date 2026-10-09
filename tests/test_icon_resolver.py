import hashlib
import importlib
import importlib.util
import json
import os
import stat
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(ICON_SOURCE='yoto-library-ai',
            ICON_CACHE_DIR=Path(self.tmp.name) / 'icons',
            ICON_AI_MAX_GENERATIONS=1, REPLICATE_API_TOKEN='test-not-a-secret')
        self.client = Mock()
        self.client.upload_icon.return_value = 'generated-id'

    def resolver(self):
        self.assertIsNotNone(importlib.util.find_spec('icon_resolver'), 'resolver feature missing')
        return importlib.import_module('icon_resolver').IconResolver(self.client, self.cfg)

    def run_with_ai(self, operation):
        r = self.resolver()
        with patch('icon_resolver.find_library_icon', return_value=None), \
             patch('art_generator.extract_subject', return_value='moon') as subject, \
             patch('art_generator.generate_pixel_art', return_value=b'png') as art:
            operation(r, subject, art)

    def test_library_hit_never_generates(self):
        r = self.resolver()
        with patch('icon_resolver.find_library_icon', return_value='library-id'), patch('art_generator.generate_pixel_art') as art:
            self.assertEqual(r.resolve('Moon', 'moon'), 'library-id')
            art.assert_not_called()
            self.client.upload_icon.assert_not_called()

    def test_disabled_hybrid_never_calls_ai(self):
        for token, cap in [('', 1), ('token', 0)]:
            self.cfg.REPLICATE_API_TOKEN, self.cfg.ICON_AI_MAX_GENERATIONS = token, cap
            self.run_with_ai(lambda r, s, a: (self.assertIsNone(r.resolve('Moon', 'm')), s.assert_not_called(), a.assert_not_called()))

    def test_library_error_does_not_trigger_paid_fallback(self):
        r = self.resolver()
        with patch('icon_resolver.find_library_icon', side_effect=RuntimeError('secret')), patch('art_generator.generate_pixel_art') as art:
            self.assertIsNone(r.resolve('Moon', 'm'))
            art.assert_not_called()

    def test_cached_id_survives_restart_normalized_title(self):
        def check(r, s, a):
            self.assertEqual(r.resolve('  MOON  song ', 'm'), 'generated-id')
            self.assertEqual(self.resolver().resolve('moon song', 'other'), 'generated-id')
            self.assertEqual(a.call_count, 1)
            self.assertEqual(self.client.upload_icon.call_count, 1)
        self.run_with_ai(check)

    def test_upload_retry_reuses_persisted_png(self):
        self.client.upload_icon.side_effect = [RuntimeError('secret'), 'uploaded']
        def check(r, s, a):
            self.assertIsNone(r.resolve('Moon', 'm'))
            self.assertEqual(self.resolver().resolve('Moon', 'm'), 'uploaded')
            self.assertEqual(a.call_count, 1)
            self.assertEqual(s.call_count, 1)
        self.run_with_ai(check)

    def test_failed_generation_reserves_budget_and_never_retries(self):
        def check(r, s, a):
            def fail(*args, **kwargs):
                ledger = json.loads((self.cfg.ICON_CACHE_DIR / 'cache.json').read_text())
                self.assertEqual(ledger['attempts'], 1)
                raise RuntimeError('secret')
            a.side_effect = fail
            self.assertIsNone(r.resolve('Moon', 'm'))
            self.assertIsNone(self.resolver().resolve('Moon', 'm'))
            self.assertIsNone(self.resolver().resolve('Other', 'o'))
            self.assertEqual(a.call_count, 1)
        self.run_with_ai(check)

    def test_settings_change_invalidates_cache(self):
        self.cfg.ICON_AI_MAX_GENERATIONS = 2
        def check(r, s, a):
            import art_generator
            self.assertEqual(r.resolve('Moon', 'm'), 'generated-id')
            with patch.object(art_generator, 'STYLE', 'different'):
                self.assertEqual(self.resolver().resolve('Moon', 'm'), 'generated-id')
            self.assertEqual(a.call_count, 2)
        self.run_with_ai(check)

    def track_directory_fsyncs(self, events, fail_on=None):
        real_open, real_fsync = os.open, os.fsync
        directories = {}

        def tracked_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            directories.pop(fd, None)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                directories[fd] = Path(path)
            return fd

        def tracked_fsync(fd):
            directory = directories.get(fd)
            events.append(('directory', directory) if directory is not None else ('file', None))
            if directory is not None and directory == fail_on:
                raise OSError('directory fsync failed')
            return real_fsync(fd)

        return patch.multiple('icon_resolver.os', open=tracked_open, fsync=tracked_fsync)

    def test_new_nested_cache_directory_chain_is_durable_before_paid_work(self):
        cache = Path(self.tmp.name) / 'new' / 'nested' / 'icons'
        self.cfg.ICON_CACHE_DIR = cache
        events = []

        def check(r, subject, art):
            subject.side_effect = lambda *args: events.append(('subject', None)) or 'moon'
            art.side_effect = lambda *args, **kwargs: events.append(('generate', None)) or b'png'
            self.client.upload_icon.side_effect = lambda *args, **kwargs: events.append(('upload', None)) or 'generated-id'
            with self.track_directory_fsyncs(events):
                self.assertEqual(r.resolve('Moon', 'm'), 'generated-id')
            self.assertEqual(events[:events.index(('subject', None))],
                [('directory', path) for path in [cache, *cache.parents]] +
                [('file', None), ('directory', cache)])
            self.assertLess(events.index(('subject', None)), events.index(('generate', None)))
            self.assertLess(events.index(('generate', None)), events.index(('upload', None)))
            ledger = json.loads((cache / 'cache.json').read_text())
            self.assertEqual(ledger['attempts'], 1)
            self.assertEqual(len(ledger['entries']), 1)
        self.run_with_ai(check)

    def test_each_new_cache_directory_fsync_failure_prevents_paid_work_and_reservation(self):
        for level in range(len((Path(self.tmp.name) / 'case' / 'nested' / 'icons').parents) + 1):
            with self.subTest(level=level):
                cache = Path(self.tmp.name) / f'case-{level}' / 'nested' / 'icons'
                self.cfg.ICON_CACHE_DIR = cache
                chain = [cache, *cache.parents]
                events = []
                self.client.reset_mock()

                def check(r, subject, art):
                    with self.track_directory_fsyncs(events, fail_on=chain[level]):
                        result = r.resolve('Moon', 'm')
                    subject.assert_not_called()
                    art.assert_not_called()
                    self.client.upload_icon.assert_not_called()
                    self.assertIsNone(result)
                    self.assertEqual(events, [('directory', path) for path in chain[:level + 1]])
                    self.assertFalse((cache / 'cache.json').exists())
                    self.assertFalse(list(cache.glob('*.png')))
                self.run_with_ai(check)

    def test_existing_cache_directory_full_chain_is_durable_before_paid_work(self):
        cache = self.cfg.ICON_CACHE_DIR
        cache.mkdir()
        events = []

        def check(r, subject, art):
            subject.side_effect = lambda *args: events.append(('subject', None)) or 'moon'
            with self.track_directory_fsyncs(events):
                self.assertEqual(r.resolve('Moon', 'm'), 'generated-id')
            self.assertEqual(events[:events.index(('subject', None))],
                [('directory', path) for path in [cache, *cache.parents]] +
                [('file', None), ('directory', cache)])
        self.run_with_ai(check)

    def test_existing_cache_directory_fsync_failure_preserves_attempt_ledger(self):
        cache = self.cfg.ICON_CACHE_DIR
        cache.mkdir()
        self.cfg.ICON_AI_MAX_GENERATIONS = 2
        ledger_path = cache / 'cache.json'
        original = json.dumps({'attempts': 1, 'entries': {'old': {'status': 'reserved'}}})
        ledger_path.write_text(original)
        events = []

        def check(r, subject, art):
            chain = [cache, *cache.parents]
            for level, directory in enumerate(chain):
                with self.subTest(directory=directory):
                    events.clear()
                    with self.track_directory_fsyncs(events, fail_on=directory):
                        self.assertIsNone(r.resolve('Moon', 'm'))
                        self.assertIsNone(self.resolver().resolve('Moon', 'm'))
                    subject.assert_not_called()
                    art.assert_not_called()
                    self.client.upload_icon.assert_not_called()
                    self.assertEqual(ledger_path.read_text(), original)
                    self.assertEqual(events, [('directory', path) for path in chain[:level + 1]] * 2)
        self.run_with_ai(check)

    def test_ancestor_fsync_failure_stays_closed_on_retry_and_recreated_resolver(self):
        cache = Path(self.tmp.name) / 'new' / 'nested' / 'icons'
        self.cfg.ICON_CACHE_DIR = cache
        events = []
        failed_ancestor = cache.parent

        def check(r, subject, art):
            for retry in range(3):
                attempt = self.resolver() if retry == 2 else r
                events.clear()
                with self.track_directory_fsyncs(events, fail_on=failed_ancestor):
                    result = attempt.resolve('Moon', 'm')
                subject.assert_not_called()
                art.assert_not_called()
                self.client.upload_icon.assert_not_called()
                self.assertIsNone(result)
                self.assertTrue(cache.is_dir())  # mkdir survives the failed fsync
                self.assertEqual(events, [('directory', cache), ('directory', failed_ancestor)])
                self.assertFalse((cache / 'cache.json').exists())
                self.assertFalse(list(cache.glob('*.png')))
        self.run_with_ai(check)

    def test_successful_retry_syncs_full_chain_before_reservation_and_paid_work(self):
        for recreate in (False, True):
            with self.subTest(recreate=recreate):
                cache = Path(self.tmp.name) / f'retry-{recreate}' / 'nested' / 'icons'
                self.cfg.ICON_CACHE_DIR = cache
                events = []
                self.client.reset_mock()

                def check(r, subject, art):
                    with self.track_directory_fsyncs(events, fail_on=cache.parent):
                        self.assertIsNone(r.resolve('Moon', 'm'))
                    subject.assert_not_called()
                    art.assert_not_called()
                    self.client.upload_icon.assert_not_called()
                    self.assertFalse((cache / 'cache.json').exists())
                    events.clear()
                    subject.side_effect = lambda *args: events.append(('subject', None)) or 'moon'
                    art.side_effect = lambda *args, **kwargs: events.append(('generate', None)) or b'png'
                    self.client.upload_icon.side_effect = lambda *args, **kwargs: events.append(('upload', None)) or 'generated-id'
                    retry = self.resolver() if recreate else r
                    with self.track_directory_fsyncs(events):
                        self.assertEqual(retry.resolve('Moon', 'm'), 'generated-id')
                    self.assertEqual(events[:events.index(('subject', None))],
                        [('directory', path) for path in [cache, *cache.parents]] +
                        [('file', None), ('directory', cache)])
                    subject.assert_called_once()
                    art.assert_called_once()
                    self.client.upload_icon.assert_called_once()
                    self.assertLess(events.index(('subject', None)), events.index(('generate', None)))
                    self.assertLess(events.index(('generate', None)), events.index(('upload', None)))
                    ledger = json.loads((cache / 'cache.json').read_text())
                    self.assertEqual(ledger['attempts'], 1)
                    self.assertEqual(len(ledger['entries']), 1)
                self.run_with_ai(check)

    def test_relative_cache_path_syncs_absolute_chain_through_root(self):
        self.cfg.ICON_CACHE_DIR = Path('relative') / 'icons'
        cache = Path(self.tmp.name) / self.cfg.ICON_CACHE_DIR
        events = []

        def check(r, subject, art):
            subject.side_effect = lambda *args: events.append(('subject', None)) or 'moon'
            cwd = Path.cwd()
            try:
                os.chdir(self.tmp.name)
                with self.track_directory_fsyncs(events):
                    self.assertEqual(r.resolve('Moon', 'm'), 'generated-id')
            finally:
                os.chdir(cwd)
            self.assertEqual(events[:events.index(('subject', None))],
                [('directory', path) for path in [cache, *cache.parents]] +
                [('file', None), ('directory', cache)])
        self.run_with_ai(check)

    def test_private_cache_permissions(self):
        def check(r, s, a):
            r.resolve('Moon', 'm')
            for p in [self.cfg.ICON_CACHE_DIR, *self.cfg.ICON_CACHE_DIR.iterdir()]:
                self.assertEqual(p.stat().st_mode & 0o077, 0)
        self.run_with_ai(check)

    def test_corrupt_budget_fails_closed(self):
        self.cfg.ICON_CACHE_DIR.mkdir()
        (self.cfg.ICON_CACHE_DIR / 'cache.json').write_text('{broken')
        self.run_with_ai(lambda r, s, a: (self.assertIsNone(r.resolve('Moon', 'm')), a.assert_not_called()))

    def test_inconsistent_budget_fails_closed(self):
        self.cfg.ICON_CACHE_DIR.mkdir()
        (self.cfg.ICON_CACHE_DIR / 'cache.json').write_text(json.dumps(
            {'attempts': 0, 'entries': {'old': {'status': 'reserved'}}}))
        self.run_with_ai(lambda r, s, a: (self.assertIsNone(r.resolve('Moon', 'm')), a.assert_not_called()))

    def assert_corrupt_entry_fails_closed(self, entry, current_key=True):
        import art_generator
        from icon_resolver import PROMPT_STYLE
        settings = {name: getattr(art_generator, name) for name in
                    ("MODEL", "STYLE", "ICON_SIZE", "NUM_CANDIDATES", "SUBJECT_MODEL", "SUBJECT_SYSTEM", "JUDGE_MODEL")}
        key = hashlib.sha256(json.dumps({"title": "moon", "settings": settings,
            "prompt_style": PROMPT_STYLE, "version": 1}, sort_keys=True).encode()).hexdigest()
        self.cfg.ICON_AI_MAX_GENERATIONS = 2
        self.cfg.ICON_CACHE_DIR = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'icons'
        self.cfg.ICON_CACHE_DIR.mkdir()
        self.client.reset_mock()
        ledger_path = self.cfg.ICON_CACHE_DIR / 'cache.json'
        original = json.dumps({'attempts': 1, 'entries': {key if current_key else 'old': entry}})
        ledger_path.write_text(original)
        artifact = self.cfg.ICON_CACHE_DIR / 'preserved.png'
        artifact.write_bytes(b'cached-png')
        def check(r, subject, art):
            result = r.resolve('Moon', 'm')
            subject.assert_not_called()
            art.assert_not_called()
            self.client.upload_icon.assert_not_called()
            self.assertIsNone(result)
            self.assertIsNone(self.resolver().resolve('Moon', 'm'))
            self.assertIsNone(self.resolver().resolve('Other', 'o'))
            subject.assert_not_called()
            art.assert_not_called()
            self.client.upload_icon.assert_not_called()
            self.assertEqual(ledger_path.read_text(), original)
            self.assertEqual(artifact.read_bytes(), b'cached-png')
        self.run_with_ai(check)

    def test_empty_entry_never_retries_paid_generation(self):
        self.assert_corrupt_entry_fails_closed({})

    def test_null_entry_never_retries_paid_generation(self):
        self.assert_corrupt_entry_fails_closed(None)

    def test_uploaded_entry_requires_nonempty_string_id(self):
        for media_id in (None, '', 123, [], {}):
            with self.subTest(media_id=media_id):
                self.assert_corrupt_entry_fails_closed({'status': 'uploaded', 'media_id': media_id})

    def test_corrupt_unrelated_entries_block_paid_generation(self):
        for entry in ({}, None, {'status': 'unknown'}, {'status': 'uploaded'}):
            with self.subTest(entry=entry):
                self.assert_corrupt_entry_fails_closed(entry, current_key=False)

    def test_reservation_write_failure_prevents_any_paid_step(self):
        r = self.resolver()
        with patch('icon_resolver.find_library_icon', return_value=None), patch('icon_resolver._atomic_write', side_effect=OSError('secret')), patch('art_generator.extract_subject') as subject:
            self.assertIsNone(r.resolve('Moon', 'm'))
            subject.assert_not_called()

    def test_main_locks_before_state_and_keeps_lock_during_run(self):
        import main
        from runtime_lock import pipeline_lock
        state = Path(self.tmp.name) / 'state.json'
        def run():
            with self.assertRaises(RuntimeError):
                with pipeline_lock(state):
                    pass
        with patch.object(main.Config, 'validate'), patch.object(main.Config, 'STATE_FILE', state), patch.object(main, '_run_pipeline', side_effect=run) as runner:
            main.main()
            runner.assert_called_once()

    def test_hybrid_failure_allows_audio_processing(self):
        import main
        state = Mock()
        state.get.return_value = {}
        audio = {'sha256': 'sha', 'trackUrl': 'yoto:#sha'}
        self.client.upload_audio.return_value = audio
        self.client.get_card_and_chapters.return_value = ('card', [])
        self.client.save_chapters.return_value = 'card'
        with patch.object(main, 'resolve_icon', return_value=None), patch.object(main, 'wait_for_stable_file'), patch.object(main, 'read_tags', return_value=('Moon', None)), patch.object(main.shutil, 'move'), patch.object(main.Config, 'PROCESSED_DIR', Path(self.tmp.name) / 'processed'):
            main.process_file(Path(self.tmp.name) / 'song.mp3', self.client, state)
        self.client.upload_audio.assert_called_once()
        self.client.save_chapters.assert_called_once()

    def test_legacy_modes(self):
        self.cfg.ICON_SOURCE = 'none'
        r = self.resolver()
        with patch('icon_resolver.find_library_icon') as library:
            self.assertIsNone(r.resolve('Moon', 'm'))
            library.assert_not_called()
        self.cfg.ICON_SOURCE = 'yoto-library'
        self.run_with_ai(lambda r, s, a: (self.assertIsNone(r.resolve('Moon', 'm')), a.assert_not_called()))
        self.cfg.ICON_SOURCE = 'generate'
        self.cfg.ICON_AI_MAX_GENERATIONS = 0
        self.run_with_ai(lambda r, s, a: self.assertEqual(r.resolve('Moon', 'm'), 'generated-id'))

    def test_invalid_mode_and_cap_rejected(self):
        for mode, cap in [('typo', 0), ('none', -1), ('none', 'oops'), ('none', True)]:
            self.cfg.ICON_SOURCE, self.cfg.ICON_AI_MAX_GENERATIONS = mode, cap
            self.assertIsNotNone(importlib.util.find_spec('icon_resolver'), 'resolver feature missing')
            with self.assertRaises((ValueError, RuntimeError)):
                importlib.import_module('icon_resolver').IconResolver(self.client, self.cfg)

    def test_shared_lock_conflict_and_release(self):
        self.assertIsNotNone(importlib.util.find_spec('runtime_lock'), 'runtime lock missing')
        from runtime_lock import pipeline_lock
        state = Path(self.tmp.name) / 'state.json'
        with pipeline_lock(state):
            with self.assertRaises(RuntimeError):
                with pipeline_lock(state):
                    self.fail('conflicting lock acquired')
        with pipeline_lock(state):
            pass
        self.assertFalse(state.exists())

    def test_main_public_signature_reuses_resolver(self):
        self.assertIsNotNone(importlib.util.find_spec('icon_resolver'), 'resolver feature missing')
        import main
        with patch.object(main, 'IconResolver') as cls:
            client = Mock()
            main.resolve_icon(client, 'Moon', 'm')
            main.resolve_icon(client, 'Moon', 'm')
            self.assertEqual(cls.call_count, 1)


if __name__ == '__main__':
    unittest.main()

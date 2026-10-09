"""Real library/resolver regressions; only client and AI provider are mocked."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

import icon_library
from icon_resolver import IconResolver


VALID_ICON = {'mediaId': 'library-id', 'title': 'Moon', 'publicTags': ['SPACE']}
MALFORMED_RESPONSES = [
    ('empty object', {}),
    ('null response', None),
    ('string response', ''),
    ('tuple response', ()),
    ('object response', {'displayIcons': []}),
    ('null record', [None]),
    ('string record', ['moon']),
    ('list record', [[]]),
    ('missing ID', [{'title': 'Moon'}]),
    *[(f'bad ID {value!r}', [{'mediaId': value, 'title': 'Moon'}])
      for value in (None, '', '   ', 123, True, [], {})],
    *[(f'bad title {value!r}', [{'mediaId': 'bad-id', 'title': value}])
      for value in (None, 123, True, [], {})],
    *[(f'bad tags {value!r}', [{'mediaId': 'bad-id', 'publicTags': value}])
      for value in (None, 'moon', ('moon',), {'moon': True}, 123, ['moon', None], ['moon', 123])],
    ('corrupt record after match', [VALID_ICON, {'title': 'Unrelated'}]),
    ('corrupt tags after match', [VALID_ICON, {'mediaId': 'other', 'publicTags': 'moon'}]),
]


class LibraryTests(unittest.TestCase):
    def setUp(self):
        previous_cache = icon_library._library_cache
        self.addCleanup(setattr, icon_library, '_library_cache', previous_cache)
        icon_library._library_cache = None
        self.client = Mock()

    def test_malformed_responses_raise_without_caching(self):
        for label, response in MALFORMED_RESPONSES:
            with self.subTest(response=label):
                icon_library._library_cache = None
                self.client.list_library_icons.return_value = response
                with self.assertRaises((ValueError, TypeError, AttributeError)):
                    icon_library.find_library_icon(self.client, 'Moon')
                self.assertIsNone(icon_library._library_cache)

    def test_failure_does_not_cache_partial_library_and_retry_succeeds(self):
        self.client.list_library_icons.side_effect = [
            [VALID_ICON, {'title': 'Unrelated'}], [VALID_ICON]]
        with self.assertRaises((ValueError, TypeError, AttributeError)):
            icon_library.find_library_icon(self.client, 'Moon')
        self.assertIsNone(icon_library._library_cache)
        self.assertEqual(icon_library.find_library_icon(self.client, 'Moon'), 'library-id')
        self.assertEqual(self.client.list_library_icons.call_count, 2)

    def test_valid_empty_list_is_cached_no_match(self):
        self.client.list_library_icons.return_value = []
        self.assertIsNone(icon_library.find_library_icon(self.client, 'Moon'))
        self.assertEqual(icon_library._library_cache, [])
        self.assertIsNone(icon_library.find_library_icon(self.client, 'Moon'))
        self.client.list_library_icons.assert_called_once_with()

    def test_valid_records_preserve_title_tags_optional_fields_and_best_match(self):
        self.client.list_library_icons.return_value = [
            {'mediaId': 'untagged'},
            {'mediaId': 'title-id', 'title': 'Moon'},
            {'mediaId': 'tag-id', 'publicTags': ['SPACE']},
            {'mediaId': 'best-id', 'title': 'Moon', 'publicTags': ['SPACE']},
        ]
        self.assertEqual(icon_library.find_library_icon(self.client, 'Moon'), 'title-id')
        self.assertEqual(icon_library.find_library_icon(self.client, 'Space'), 'tag-id')
        self.assertEqual(icon_library.find_library_icon(self.client, 'Moon Space'), 'best-id')
        self.assertIsNone(icon_library.find_library_icon(self.client, 'Ocean'))
        self.client.list_library_icons.assert_called_once_with()


class LibraryResolverTests(unittest.TestCase):
    def setUp(self):
        previous_cache = icon_library._library_cache
        self.addCleanup(setattr, icon_library, '_library_cache', previous_cache)
        icon_library._library_cache = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg: Any = SimpleNamespace(
            ICON_SOURCE='yoto-library-ai', ICON_AI_MAX_GENERATIONS=1,
            REPLICATE_API_TOKEN='test-not-a-secret',
            ICON_CACHE_DIR=Path(self.tmp.name) / 'icons')
        self.client = Mock()
        self.client.upload_icon.return_value = 'generated-id'
        self.resolver = IconResolver(self.client, self.cfg)
        self.subject = self.enterContext(patch('art_generator.extract_subject', return_value='moon'))
        self.art = self.enterContext(patch('art_generator.generate_pixel_art', return_value=b'png'))

    def test_malformed_library_never_calls_paid_provider_or_uploads(self):
        for index, (label, response) in enumerate(MALFORMED_RESPONSES):
            with self.subTest(response=label):
                icon_library._library_cache = None
                self.cfg.ICON_CACHE_DIR = Path(self.tmp.name) / str(index)
                self.client.list_library_icons.return_value = response
                self.client.reset_mock()
                self.subject.reset_mock()
                self.art.reset_mock()
                result = self.resolver.resolve('Moon', 'moon')
                self.subject.assert_not_called()
                self.art.assert_not_called()
                self.client.upload_icon.assert_not_called()
                self.assertIsNone(result)
                self.assertIsNone(icon_library._library_cache)
                self.assertFalse(self.cfg.ICON_CACHE_DIR.exists())

    def test_empty_query_malformed_library_never_calls_paid_provider_or_uploads(self):
        for title in ('The', '1 2 3'):
            for label, response in (('empty object', {}), ('missing ID', [{'title': 'Moon'}])):
                with self.subTest(title=title, response=label):
                    icon_library._library_cache = None
                    self.cfg.ICON_CACHE_DIR = Path(self.tmp.name) / title / label
                    self.client.list_library_icons.return_value = response
                    self.client.reset_mock()
                    self.subject.reset_mock()
                    self.art.reset_mock()
                    result = self.resolver.resolve(title, 'empty-query')
                    self.subject.assert_not_called()
                    self.art.assert_not_called()
                    self.client.upload_icon.assert_not_called()
                    self.client.list_library_icons.assert_called_once_with()
                    self.assertIsNone(result)
                    self.assertIsNone(icon_library._library_cache)
                    self.assertFalse(self.cfg.ICON_CACHE_DIR.exists())

    def test_empty_query_library_error_never_calls_paid_provider_or_uploads(self):
        for title in ('The', '1 2 3'):
            with self.subTest(title=title):
                icon_library._library_cache = None
                self.cfg.ICON_CACHE_DIR = Path(self.tmp.name) / title
                self.client.list_library_icons.side_effect = RuntimeError('library unavailable')
                self.client.reset_mock()
                self.subject.reset_mock()
                self.art.reset_mock()
                result = self.resolver.resolve(title, 'empty-query')
                self.subject.assert_not_called()
                self.art.assert_not_called()
                self.client.upload_icon.assert_not_called()
                self.client.list_library_icons.assert_called_once_with()
                self.assertIsNone(result)
                self.assertIsNone(icon_library._library_cache)
                self.assertFalse(self.cfg.ICON_CACHE_DIR.exists())

    def test_empty_query_valid_empty_library_allows_guarded_generation(self):
        for title, other_title in (('The', '1 2 3'), ('1 2 3', 'The')):
            with self.subTest(title=title):
                icon_library._library_cache = None
                self.cfg.ICON_CACHE_DIR = Path(self.tmp.name) / title
                self.client.list_library_icons.return_value = []
                self.client.reset_mock()
                self.subject.reset_mock()
                self.art.reset_mock()
                self.assertEqual(self.resolver.resolve(title, 'empty-query'), 'generated-id')
                self.client.list_library_icons.assert_called_once_with()
                self.assertEqual(icon_library._library_cache, [])
                self.subject.assert_called_once_with(title, 'test-not-a-secret')
                self.art.assert_called_once()
                self.client.upload_icon.assert_called_once_with(b'png', filename='empty-query.png')
                self.assertIsNone(self.resolver.resolve(other_title, 'over-cap'))
                self.subject.assert_called_once()
                self.art.assert_called_once()
                self.client.upload_icon.assert_called_once()
                self.client.list_library_icons.assert_called_once_with()

    def test_malformed_library_retry_uses_valid_library_without_paid_calls(self):
        self.client.list_library_icons.side_effect = [{}, [VALID_ICON]]
        result = self.resolver.resolve('Moon', 'moon')
        self.subject.assert_not_called()
        self.art.assert_not_called()
        self.assertIsNone(result)
        self.assertIsNone(icon_library._library_cache)
        self.assertEqual(self.resolver.resolve('Moon', 'moon'), 'library-id')
        self.assertEqual(self.client.list_library_icons.call_count, 2)
        self.subject.assert_not_called()
        self.art.assert_not_called()
        self.client.upload_icon.assert_not_called()

    def test_valid_library_hit_never_calls_paid_provider(self):
        self.client.list_library_icons.return_value = [VALID_ICON]
        self.assertEqual(self.resolver.resolve('Moon Space', 'moon'), 'library-id')
        self.subject.assert_not_called()
        self.art.assert_not_called()
        self.client.upload_icon.assert_not_called()

    def test_valid_empty_and_unmatched_libraries_allow_guarded_generation(self):
        for index, response in enumerate(([], [{'mediaId': 'other', 'title': 'Ocean'}])):
            with self.subTest(response=response):
                icon_library._library_cache = None
                self.cfg.ICON_CACHE_DIR = Path(self.tmp.name) / str(index)
                self.client.list_library_icons.return_value = response
                self.client.reset_mock()
                self.subject.reset_mock()
                self.art.reset_mock()
                self.assertEqual(self.resolver.resolve('Moon', 'moon'), 'generated-id')
                self.subject.assert_called_once_with('Moon', 'test-not-a-secret')
                self.art.assert_called_once()
                self.client.upload_icon.assert_called_once_with(b'png', filename='moon.png')


if __name__ == '__main__':
    unittest.main()

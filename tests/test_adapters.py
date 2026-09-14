import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from context_archive.adapters import Source, discover, normalize


def row(key, payload, version=1):
    return {'row_key': str(key), 'payload': payload, 'version_id': version}


class AdapterTests(unittest.TestCase):
    def test_discovery_profiles_aliases_and_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ids = ['AAAAAAAA-1111-2222-3333-444444444444', 'AAAAAAAA-1111-2222-3333-555555555555']
            for ident in ids:
                p = home / 'Library/Safari/Profiles' / ident / 'History.db'
                p.parent.mkdir(parents=True); p.touch()
            root = home / 'Library/Application Support/Google/Chrome'
            p = root / 'Default/History'; p.parent.mkdir(parents=True); p.touch()
            alias = home / 'alias'; alias.symlink_to(p)
            result = discover(home, {'sources': [{'path': str(alias), 'adapter': 'chromium'}]})
            self.assertEqual(len(result['sources']), 3)
            self.assertEqual({s.profile for s in result['sources'] if s.adapter == 'safari'}, set(ids))
            p2 = root / 'Profile 77/History'; p2.parent.mkdir(); p2.touch()
            self.assertEqual(len(discover(home)['sources']), 4)
            self.assertEqual(len(discover(home, {'exclusions': [str(root / '*') ]})['sources']), 2)

    def test_known_container_and_chromium_roots(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            paths = [
                'Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles/full-profile-id/History.db',
                'Library/Containers/com.apple.Safari/Data/Library/WebKit/WebsiteData/other-profile-id/History.db',
                'Library/Application Support/Dia/User Data/Profile 9/History',
                'Library/Application Support/Arc/User Data/Default/History',
            ]
            for rel in paths:
                p = home / rel; p.parent.mkdir(parents=True); p.touch()
            result = discover(home)
            self.assertEqual(len(result['sources']), 4)
            self.assertEqual({s.browser for s in result['sources']}, {'Safari', 'Dia', 'Arc'})
            self.assertTrue(discover(home, {'sources': [{'path': str(home / 'missing'), 'adapter': 'safari'}]})['issues'])

    def test_discovery_failure_visible(self):
        with patch.object(Path, 'iterdir', side_effect=PermissionError('denied')):
            result = discover(Path('/fake'))
        self.assertTrue(result['issues'])

    def test_chromium_integer_precision_and_all_visits(self):
        stamp = 13300000000000001
        rows = {'urls': [row(1, {'id': 8, 'url': 'https://example.com/a', 'title': 'Title'}, 2)],
                'visits': [row(n, {'id': n, 'url': 8, 'visit_time': {'$integer': str(stamp)}, 'originator_cache_guid': 'raw-guid'}, n + 10) for n in range(1200)]}
        events, warnings = normalize(Source(Path('/x'), 'chromium'), rows)
        self.assertEqual(len(events), 1200)
        self.assertEqual(len({e['event_key'] for e in events}), 1200)
        self.assertTrue(events[0]['started_at'].endswith('000001Z'))
        self.assertEqual(events[0]['domain'], 'example.com')
        self.assertEqual(events[0]['origin']['visit']['originator_cache_guid'], 'raw-guid')
        self.assertEqual(events[0]['raw_refs'], [10, 2])
        self.assertFalse(warnings)

    def test_knowledge_invalid_and_native_stream(self):
        events, _ = normalize(Source(Path('/x'), 'knowledge'), {'ZOBJECT': [
            row(1, {'ZUUID': 'uuid', 'ZSTREAMNAME': '/app/usage', 'ZVALUESTRING': 'app', 'ZSTARTDATE': 0}),
            row(2, {'ZSTREAMNAME': '/device/battery', 'ZVALUESTRING': 'not-app', 'ZSTARTDATE': float('inf')})]})
        self.assertEqual(events[0]['event_key'], '["ZOBJECT","uuid","1"]')
        self.assertEqual(events[0]['app_id'], 'app')
        self.assertEqual(events[1]['kind'], '/device/battery')
        self.assertIsNone(events[1]['started_at'])
        self.assertNotIn('app_id', events[1])

    def test_safari_optional_and_legacy_offsets(self):
        events, _ = normalize(Source(Path('/x'), 'safari'), {'history_items': [row(1, {'id': 3, 'url': 'https://x.test'}, 5)], 'history_visits': [row(4, {'history_item': 3, 'visit_time': 0.25, 'load_successful': 1}, 6)]})
        self.assertEqual(events[0]['started_at'], '2001-01-01T00:00:00.250000Z')
        self.assertEqual(events[0]['raw_refs'], [6, 5])
        events, warnings = normalize(Source(Path('/x'), 'legacy_browser'), {'visits': [row('a', {'timestamp': '2026-01-01T01:00:00+01:00', 'url': 'https://x.test', 'browser': 'Safari', 'device': 'phone'}), row('b', {'started_at': '2026-01-01T00:00:00'})]})
        self.assertEqual(events[0]['started_at'], '2026-01-01T00:00:00.000000Z')
        self.assertIsNone(events[1]['started_at'])
        self.assertEqual(events[0]['origin']['device'], 'phone')
        self.assertEqual(events[0]['raw_refs'], [1])
        self.assertTrue(normalize(Source(Path('/x'), 'safari'), {})[1])

    def test_invalid_tagged_text_remains_raw(self):
        event, warnings = normalize(Source(Path('/x'), 'knowledge'), {'ZOBJECT': [row('1', {'ZUUID': {'$blob': 'x'}, 'ZSTREAMNAME': {'$text_bytes': '/w=='}})]})
        self.assertEqual(event[0]['event_key'], '["ZOBJECT",null,"1"]')
        self.assertEqual(event[0]['kind'], 'ZOBJECT')
        self.assertTrue(warnings)

    def test_reused_visit_id_is_new_event(self):
        source = Source(Path('/x'), 'chromium')
        first, _ = normalize(source, {'urls': [], 'visits': [row('1', {'visit_time': 100})]})
        second, _ = normalize(source, {'urls': [], 'visits': [row('1', {'visit_time': 101})]})
        self.assertNotEqual(first[0]['event_key'], second[0]['event_key'])

    def test_invalid_and_duplicate_uuids_disambiguated(self):
        payloads = [{'ZUUID': u, 'ZSTREAMNAME': '/app/usage', 'ZVALUESTRING': {'bad': 'type'}} for u in ['', '', 'dup', 'dup', 'unique']]
        events, warnings = normalize(Source(Path('/x'), 'knowledge'), {'ZOBJECT': [row(str(i), p, i + 1) for i, p in enumerate(payloads)]})
        self.assertEqual(len(events), 5)
        self.assertEqual(len({e['event_key'] for e in events}), 5)
        self.assertEqual(events[-1]['event_key'], '["ZOBJECT","unique","4"]')
        self.assertTrue(warnings)
        self.assertTrue(all(e.get('app_id') is None for e in events))

    def test_hardlink_discovery_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            first = home / 'first'; first.touch()
            second = home / 'second'; second.hardlink_to(first)
            result = discover(home, {'sources': [{'path': str(p), 'adapter': 'safari'} for p in [first, second]]})
            self.assertEqual(len(result['sources']), 1)

    def test_duplicate_uuid_capture_preserves_raw_and_events(self):
        import sqlite3
        from context_archive.core import capture, readonly
        with tempfile.TemporaryDirectory() as d:
            native, archive = Path(d) / 'native', Path(d) / 'archive'
            with sqlite3.connect(native) as c:
                c.execute('CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZUUID TEXT,ZSTREAMNAME TEXT)')
                c.executemany('INSERT INTO ZOBJECT VALUES(?,?,?)', [(i, uuid, '/app/usage') for i, uuid in enumerate(['', '', 'dup', 'dup', 'unique'])])
            result = capture(Source(native, 'knowledge'), archive)
            self.assertTrue(result['warnings'])
            with readonly(archive) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM records').fetchone()[0], 5)
                self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0], 5)

    def test_duplicate_pruning_keeps_historical_identity(self):
        import sqlite3
        from context_archive.core import capture, readonly, reindex
        with tempfile.TemporaryDirectory() as d:
            native, archive = Path(d) / 'native', Path(d) / 'archive'
            with sqlite3.connect(native) as c:
                c.execute('CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZUUID TEXT,ZSTREAMNAME TEXT,ZSTARTDATE REAL,ZENDDATE REAL)')
                c.executemany('INSERT INTO ZOBJECT VALUES(?,?,?,?,?)', [(i, 'dup', '/app/usage', i * 60, (i + 1) * 60) for i in range(2)])
            source = Source(native, 'knowledge')
            capture(source, archive)
            with readonly(archive) as c:
                before = [tuple(r) for r in c.execute('SELECT event_key,started_at,ended_at FROM events ORDER BY event_key')]
            with sqlite3.connect(native) as c:
                c.execute('DELETE FROM ZOBJECT WHERE Z_PK=0')
            capture(source, archive)
            reindex(archive)
            with readonly(archive) as c:
                after = [tuple(r) for r in c.execute('SELECT event_key,started_at,ended_at FROM events ORDER BY event_key')]
            self.assertEqual(after, before)

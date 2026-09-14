"""Bounded source discovery and rebuildable normalization; no database reads."""
from __future__ import annotations

from dataclasses import dataclass, replace
from collections import Counter
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
import json
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Source:
    path: Path
    adapter: str
    browser: str | None = None
    profile: str | None = None
    device: str | None = None


def discover(home: Path | None = None, config: dict | None = None, include_disabled: bool = False) -> dict:
    """Re-enumerate only known roots and their direct profile directories."""
    home, config = Path(home or Path.home()), config or {}
    sources, issues, seen = [], [], set()
    seen_files = set()
    exclusions = config.get('exclusions', [])
    disabled = {str(Path(p).resolve()) for p in config.get('disabled_source_paths', [])}

    def issue(path, exc):
        issues.append({'path': str(path), 'status': 'permission_denied' if isinstance(exc, PermissionError) else 'discovery_failed', 'message': str(exc)})

    def add(source, explicit=False):
        try:
            path = Path(source.path).expanduser()
            canonical = path.resolve()
            if not include_disabled and str(canonical) in disabled: return
            if any(fnmatch(str(p), pattern) for p in (path, canonical) for pattern in exclusions):
                return
            if not path.is_file():
                if explicit:
                    issue(path, FileNotFoundError('Source does not exist or is not a file'))
                return
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if canonical not in seen and identity not in seen_files:
                sources.append(replace(source, path=canonical)); seen.add(canonical)
                seen_files.add(identity)
        except (OSError, RuntimeError) as exc:
            issue(source.path, exc)

    def children(root):
        try:
            return sorted(root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as exc:
            issue(root, exc)
            return []

    add(Source(home / 'Library/Application Support/Knowledge/knowledgeC.db', 'knowledge'))
    safari_roots = [home / 'Library/Safari',
                    home / 'Library/Containers/com.apple.Safari/Data/Library/Safari',
                    home / 'Library/Containers/com.apple.Safari/Data/Library/WebKit']
    for root in safari_roots:
        add(Source(root / 'History.db', 'safari', 'Safari', 'Default'))
        for profile in children(root / 'Profiles'):
            add(Source(profile / 'History.db', 'safari', 'Safari', profile.name))
    # Safari has shipped both Safari/Profiles and WebKit/WebsiteData profile roots.
    webkit = home / 'Library/Containers/com.apple.Safari/Data/Library/WebKit/WebsiteData'
    for profile in children(webkit):
        add(Source(profile / 'History.db', 'safari', 'Safari', profile.name))
        add(Source(profile / 'Safari/History.db', 'safari', 'Safari', profile.name))
    chromium_roots = [('Chrome', 'Google/Chrome'), ('Dia', 'Dia/User Data'),
                      ('Dia', 'Dia'), ('Arc', 'Arc/User Data')]
    for browser, relative in chromium_roots:
        root = home / 'Library/Application Support' / relative
        for profile in children(root):
            add(Source(profile / 'History', 'chromium', browser, profile.name))
    for entry in config.get('sources', []):
        try:
            source = entry if isinstance(entry, Source) else Source(**entry)
            if not isinstance(source.adapter,str): raise ValueError('adapter must be a string')
            for field in ('browser','profile','device'):
                if getattr(source,field) is not None and not isinstance(getattr(source,field),str):
                    raise ValueError(field+' must be a string or null')
            if source.adapter not in {'knowledge', 'safari', 'chromium', 'legacy_browser'}:
                raise ValueError('Unknown adapter: ' + source.adapter)
            add(source, explicit=True)
        except (TypeError, ValueError) as exc:
            issue(entry.get('path',entry) if isinstance(entry,dict) else entry, exc)
    return {'sources': sources, 'issues': issues}


def _number(value):
    if isinstance(value, dict) and '$integer' in value:
        try:
            return int(value['$integer'])
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _date(value, epoch=2001, microseconds=False):
    value = _number(value)
    if value is None:
        return None
    try:
        # Never round a WebKit integer through a float: modern epochs exceed 2**53.
        delta = timedelta(microseconds=value) if microseconds else timedelta(seconds=value)
        return (datetime(epoch, 1, 1, tzinfo=timezone.utc) + delta).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError, TypeError):
        return None


def _iso(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z') if dt.utcoffset() is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _url_fields(event, url, title=None):
    if isinstance(url, str):
        event['url'] = url
        try:
            event['domain'] = urlsplit(url).hostname
        except ValueError:
            event['domain'] = None
    if isinstance(title, str):
        event['title'] = title


def normalize(source: Source, rows: dict[str, list[dict]]) -> tuple[list[dict], list[str]]:
    """Keep each native event identity, including invalid dates and unmatched joins.

    Raw payloads remain authoritative. Origin carries native metadata without
    guessing whether a synced visit represents this machine or another device.
    """
    events, warnings = [], []
    if source.adapter == 'knowledge':
        if 'ZOBJECT' not in rows:
            return [], ['Missing ZOBJECT table']
        if any(not isinstance(r['payload'].get('ZSTREAMNAME'), str) for r in rows['ZOBJECT']):
            warnings.append('ZOBJECT rows missing or invalid ZSTREAMNAME')
        counts = Counter(r['payload'].get('ZUUID') for r in rows['ZOBJECT']
                         if isinstance(r['payload'].get('ZUUID'), str) and r['payload']['ZUUID'])
        if any(not isinstance(r['payload'].get('ZUUID'), str) or not r['payload'].get('ZUUID')
                   or counts[r['payload']['ZUUID']] > 1 for r in rows['ZOBJECT']):
            warnings.append('Invalid or duplicate ZUUID; using row identity')
        for row in rows['ZOBJECT']:
            p = row['payload']; stream = p.get('ZSTREAMNAME')
            stream = stream if isinstance(stream, str) else None
            # Identity cannot depend on other currently retained rows: pruning a
            # duplicate must not create a second historical event for its sibling.
            uuid = p.get('ZUUID')
            uuid = uuid if isinstance(uuid, str) and uuid else None
            event = {'event_key': json.dumps(['ZOBJECT', uuid, row['row_key']], sort_keys=True, separators=(',', ':'), ensure_ascii=True),
                     'kind': 'app_usage' if stream == '/app/usage' else (stream or 'ZOBJECT'),
                     'started_at': _date(p.get('ZSTARTDATE')), 'ended_at': _date(p.get('ZENDDATE')),
                     'raw_refs': [row['version_id']], 'origin': p}
            if isinstance(stream, str) and stream.startswith('/app/'):
                event['app_id'] = p.get('ZVALUESTRING') if isinstance(p.get('ZVALUESTRING'), str) else None
            events.append(event)
    elif source.adapter in {'chromium', 'safari'}:
        chrome = source.adapter == 'chromium'
        visits, items = ('visits', 'urls') if chrome else ('history_visits', 'history_items')
        for name in (visits, items):
            if name not in rows:
                warnings.append('Missing ' + name + ' table')
        if any('visit_time' not in r['payload'] for r in rows.get(visits, [])):
            warnings.append(visits + ' rows missing visit_time')
        if any('id' not in r['payload'] for r in rows.get(items, [])):
            warnings.append(items + ' rows missing id')
        index = {_number(r['payload'].get('id')): r for r in rows.get(items, []) if _number(r['payload'].get('id')) is not None}
        for row in rows.get(visits, []):
            p = row['payload']; item = index.get(_number(p.get('url' if chrome else 'history_item')))
            q = item['payload'] if item else {}
            event = {'event_key': visits + ':' + row['row_key'] + ':' + json.dumps(p.get('visit_time'), sort_keys=True, separators=(',', ':')), 'kind': 'browser_visit',
                     'started_at': _date(p.get('visit_time'), 1601 if chrome else 2001, chrome),
                     'ended_at': None, 'raw_refs': [row['version_id']] + ([item['version_id']] if item else []),
                     'origin': {'visit': p, 'item': q}}
            _url_fields(event, q.get('url'), p.get('title', q.get('title')))
            events.append(event)
    elif source.adapter == 'legacy_browser':
        if 'visits' not in rows:
            warnings.append('Missing visits table')
        for row in rows.get('visits', []):
            p = row['payload']
            event = {'event_key': row['row_key'], 'kind': 'browser_visit',
                     'started_at': _iso(p.get('timestamp', p.get('started_at', p.get('visit_time')))),
                     'ended_at': _iso(p.get('ended_at')), 'raw_refs': [row['version_id']], 'origin': p}
            _url_fields(event, p.get('url'), p.get('title'))
            events.append(event)
    else:
        warnings.append('Unknown adapter: ' + source.adapter)
    return events, warnings

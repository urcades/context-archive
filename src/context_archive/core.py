"""Transactional evidence store. Source snapshots and archive commits are separate.

Row bytes are deduplicated, observations are not: A -> B -> A is three changes.
Only a complete, validated snapshot can establish source deletions. Event indexes
are disposable interpretations; raw evidence is always authoritative.
"""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

SCHEMA = '''
CREATE TABLE archive_info(version INTEGER NOT NULL, archive_id TEXT NOT NULL, index_generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sources(id TEXT PRIMARY KEY, path TEXT NOT NULL, identity TEXT NOT NULL,
 adapter TEXT NOT NULL, browser TEXT, profile TEXT, device TEXT, fidelity TEXT NOT NULL);
CREATE TABLE source_paths(source_id TEXT NOT NULL REFERENCES sources(id), path TEXT NOT NULL, PRIMARY KEY(source_id,path));
CREATE TABLE captures(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 observed_at TEXT NOT NULL, schema_json TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
 changed_count INTEGER NOT NULL DEFAULT 0, deleted_count INTEGER NOT NULL DEFAULT 0,
 provenance TEXT NOT NULL DEFAULT 'native', legacy_ref TEXT UNIQUE, collected_by TEXT);
CREATE TABLE records(version_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 table_name TEXT NOT NULL, row_key TEXT NOT NULL, content_hash TEXT NOT NULL,
 first_capture INTEGER NOT NULL REFERENCES captures(id), payload_json TEXT NOT NULL,
 UNIQUE(source_id,table_name,row_key,content_hash));
CREATE TABLE changes(capture_id INTEGER NOT NULL REFERENCES captures(id), table_name TEXT NOT NULL,
 row_key TEXT NOT NULL, version_id INTEGER REFERENCES records(version_id),
 PRIMARY KEY(capture_id,table_name,row_key));
CREATE TABLE current_rows(source_id TEXT NOT NULL REFERENCES sources(id), table_name TEXT NOT NULL,
 row_key TEXT NOT NULL, version_id INTEGER NOT NULL REFERENCES records(version_id),
 PRIMARY KEY(source_id,table_name,row_key));
CREATE TABLE blobs(hash TEXT PRIMARY KEY, data BLOB NOT NULL, size INTEGER NOT NULL);
CREATE TABLE events(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 event_key TEXT NOT NULL, capture_id INTEGER NOT NULL REFERENCES captures(id),
 kind TEXT NOT NULL, started_at TEXT, ended_at TEXT, url TEXT, title TEXT, domain TEXT, app_id TEXT,
 payload_json TEXT NOT NULL, UNIQUE(source_id,event_key,capture_id));
CREATE VIRTUAL TABLE event_search USING fts5(text, tokenize='unicode61');
CREATE TABLE attempts(id INTEGER PRIMARY KEY, source_path TEXT, attempted_at TEXT NOT NULL,
 status TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE imports(id TEXT PRIMARY KEY, kind TEXT NOT NULL, imported_at TEXT NOT NULL,
 details_json TEXT NOT NULL);
CREATE TABLE import_lines(import_id TEXT NOT NULL REFERENCES imports(id), line_number INTEGER NOT NULL,
 raw BLOB NOT NULL, error TEXT, PRIMARY KEY(import_id,line_number));
CREATE INDEX records_source ON records(source_id,table_name);
CREATE INDEX captures_source ON captures(source_id,id);
CREATE INDEX changes_version ON changes(version_id,capture_id);
CREATE INDEX events_identity ON events(source_id,event_key,capture_id DESC);
CREATE INDEX events_time ON events(started_at,id);
CREATE INDEX events_domain ON events(domain,started_at);
CREATE INDEX events_app ON events(app_id,started_at);
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value,str) else value).hexdigest()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class InvalidText(bytes):
    """Keep invalid UTF-8 SQLite TEXT distinct from BLOB."""


def text_factory(value):
    try: return value.decode('utf-8')
    except UnicodeDecodeError: return InvalidText(value)


def readonly(path):
    path = Path(path).expanduser().resolve(strict=True)
    c = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def encode_value(value, archive):
    if isinstance(value, InvalidText): return {'$text_bytes':base64.b64encode(value).decode('ascii')}
    if isinstance(value, bytes):
        h=digest(value)
        archive.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)',(h,value,len(value)))
        return {'$blob':h,'size':len(value)}
    if isinstance(value,int) and abs(value)>2**53-1: return {'$integer':str(value)}
    if isinstance(value,float) and not math.isfinite(value): return {'$float':value.hex()}
    return value


def decode_value(value, archive):
    if not isinstance(value,dict): return value
    if '$blob' in value:
        row=archive.execute('SELECT data FROM blobs WHERE hash=?',(value['$blob'],)).fetchone()
        if row is None: raise ValueError('Missing blob')
        return bytes(row[0])
    if '$integer' in value: return int(value['$integer'])
    if '$float' in value: return float.fromhex(value['$float'])
    if '$text_bytes' in value: return InvalidText(base64.b64decode(value['$text_bytes']))
    return value


def initialize(path):
    path=Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600); os.close(fd)
    os.chmod(path,0o600)
    with closing(sqlite3.connect(path,timeout=30)) as c:
        existing=c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not existing:
            # Initialization is also transactional: a crash cannot leave half a schema.
            c.executescript('BEGIN IMMEDIATE;'+SCHEMA)
            c.execute('INSERT INTO archive_info(version,archive_id) VALUES(1,?)',(str(uuid.uuid4()),))
            c.commit()
        try: version=c.execute('SELECT version FROM archive_info').fetchone()
        except sqlite3.Error as e: raise ValueError('Not a Context Archive database') from e
        if version!=(1,): raise ValueError('Unsupported archive version')
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=FULL')
    return path


@contextmanager
def writer(path):
    path=Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(str(path)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        initialize(path)
        with closing(sqlite3.connect(path,timeout=30)) as c:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            c.execute('PRAGMA synchronous=FULL')
            try:
                c.execute('BEGIN IMMEDIATE')
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise


@contextmanager
def snapshot(path, timeout=60):
    # An on-disk temporary snapshot bounds RAM, and backup incorporates committed WAL.
    with tempfile.TemporaryDirectory(prefix='context-archive-') as directory:
        dest=Path(directory)/'snapshot.sqlite3'
        with closing(readonly(path)) as live, closing(sqlite3.connect(dest)) as snap:
            deadline=time.monotonic()+timeout
            def progress(status, remaining, total):
                if time.monotonic()>deadline:
                    # Only SQLite's actual busy/locked result identifies contention.
                    # A slow but progressing backup remains a generic timeout.
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise TimeoutError('Snapshot database locked at deadline')
                    raise TimeoutError('Snapshot exceeded deadline')
            live.backup(snap,pages=256,progress=progress,sleep=0.01)
            snap.row_factory=sqlite3.Row
            snap.text_factory=text_factory
            if snap.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('Snapshot integrity check failed')
            yield snap


def protect(destination, paths):
    destination=Path(destination).expanduser().resolve()
    for path in paths:
        path=Path(path).expanduser().resolve()
        for suffix in ('','-wal','-shm','-journal','.lock'):
            candidate=Path(str(path)+suffix)
            if destination==candidate or (destination.exists() and candidate.exists() and destination.samefile(candidate)):
                raise ValueError('Destination would overwrite a source, archive, or sidecar')


def identity_key(adapter, identity, path):
    if adapter=='knowledge' and (not isinstance(identity,str) or not identity.strip() or identity=='None'):
        return digest(canonical(['knowledge','legacy-path',str(Path(path).resolve())]))
    return digest(canonical([adapter,identity]))


def source_identity(source, snap):
    try:
        identity=snap.execute('SELECT Z_UUID FROM Z_METADATA LIMIT 1').fetchone()[0]
        if not isinstance(identity,(str,bytes)) or not identity or identity=='None':
            raise ValueError('Invalid persistent store identity')
        identity=identity.hex() if isinstance(identity,bytes) else identity
        if not identity.strip():raise ValueError('Empty persistent store identity')
    except (sqlite3.Error,TypeError,ValueError):
        st=Path(source.path).stat()
        # macOS birth time distinguishes inode reuse and does not change on writes.
        # On systems without birth time the inode remains the fallback identity.
        identity=f'file:{st.st_dev}:{st.st_ino}:{getattr(st,"st_birthtime",0)}'
    return identity_key(source.adapter,identity,source.path),identity


def register_source(c, source, sid, identity, fidelity='raw_database'):
    c.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?,?,?,?)',
              (sid,str(Path(source.path).expanduser().resolve()),identity,source.adapter,
               source.browser,source.profile,source.device,fidelity))
    c.execute('INSERT OR IGNORE INTO source_paths VALUES(?,?)',(sid,str(Path(source.path).expanduser().resolve())))


def snapshot_rows(snap, table, archive, omit_blobs=False):
    cols=snap.execute('PRAGMA table_xinfo('+quote(table)+')').fetchall()
    names=[r['name'] for r in cols if r['hidden']!=1]
    pk=[r['name'] for r in sorted(cols,key=lambda r:r['pk']) if r['pk']]
    rowid=next((n for n in ('_rowid_','rowid','oid') if n not in {x.lower() for x in names}),None)
    if rowid:
        try: snap.execute('SELECT '+rowid+' FROM '+quote(table)+' LIMIT 0')
        except sqlite3.OperationalError: rowid=None
    projection=','.join(quote(n) for n in names)
    for index,row in enumerate(snap.execute('SELECT '+(rowid+',' if rowid else '')+projection+' FROM '+quote(table))):
        offset=int(rowid is not None)
        # Tab sync records contain opaque CloudKit payloads; preserve lightweight
        # metadata and change detection without persisting those binary contents.
        def encode(value):
            if omit_blobs and isinstance(value, bytes):
                return {'$omitted_blob': {'sha256': digest(value), 'size': len(value), 'policy': 'metadata-v1'}}
            return encode_value(value, archive)
        payload={n:encode(row[i+offset]) for i,n in enumerate(names)}
        if pk and all(payload[n] is not None for n in pk): key=[payload[n] for n in pk]
        elif rowid: key=['$rowid',encode_value(row[0],archive)]
        else: key=['$ordinal',index]
        yield canonical(key),canonical(payload)


def observe(c, sid, cid, table, key, payload):
    h=digest(payload)
    c.execute('INSERT OR IGNORE INTO records(source_id,table_name,row_key,content_hash,first_capture,payload_json) VALUES(?,?,?,?,?,?)',
              (sid,table,key,h,cid,payload))
    vid=c.execute('SELECT version_id FROM records WHERE source_id=? AND table_name=? AND row_key=? AND content_hash=?',
                  (sid,table,key,h)).fetchone()[0]
    c.execute('INSERT INTO changes VALUES(?,?,?,?)',(cid,table,key,vid))
    c.execute('INSERT OR REPLACE INTO current_rows VALUES(?,?,?,?)',(sid,table,key,vid))
    return vid


def current_payloads(c,sid):
    latest=c.execute('SELECT schema_json FROM captures WHERE source_id=? ORDER BY id DESC LIMIT 1',(sid,)).fetchone()
    rows={t['name']:[] for t in json.loads(latest[0]) if t.get('type')=='table'} if latest else {}
    for r in c.execute('SELECT r.* FROM current_rows cr JOIN records r USING(version_id) WHERE cr.source_id=?',(sid,)):
        rows.setdefault(r['table_name'],[]).append({'row_key':r['row_key'],'version_id':r['version_id'],'payload':json.loads(r['payload_json'])})
    return rows


def project(c, source, sid, cid, rows=None):
    from .adapters import normalize
    rows=rows if rows is not None else current_payloads(c,sid)
    # A visit can outlive its joined URL row. Supplement missing join rows from
    # the latest original evidence at this capture, never from a future snapshot.
    item_table={'safari':'history_items','chromium':'urls'}.get(source.adapter)
    historical=[]
    if item_table:
        present={r['row_key'] for r in rows.get(item_table,[])}
        for r in c.execute("""SELECT r.* FROM records r JOIN changes ch ON ch.version_id=r.version_id
          WHERE r.source_id=? AND r.table_name=? AND ch.capture_id<=?
          AND NOT EXISTS (SELECT 1 FROM changes n JOIN records nr ON nr.version_id=n.version_id
            WHERE nr.source_id=r.source_id AND nr.table_name=r.table_name AND nr.row_key=r.row_key
              AND n.capture_id>ch.capture_id AND n.capture_id<=?)""",(sid,item_table,cid,cid)):
            if r['row_key'] not in present:
                rows.setdefault(item_table,[]).append({'row_key':r['row_key'],'version_id':r['version_id'],'payload':json.loads(r['payload_json'])})
                present.add(r['row_key']);historical.append(r['version_id'])
    events,warnings=normalize(source,rows)
    capture_schema = json.loads(c.execute('SELECT schema_json FROM captures WHERE id=?', (cid,)).fetchone()[0])
    acquisition = next((r['metadata'] for r in capture_schema if r.get('type') == 'acquisition'), None)
    if acquisition:
        for event in events: event['acquisition'] = acquisition
    if historical:
        for event in events:
            refs=[ref for ref in event.get('raw_refs',[]) if ref in historical]
            if refs:event['historical_join_refs']=refs
    for event in events:
        if source.adapter == 'safari_tabs':
            # Each successful snapshot is a new observation, including unchanged
            # tabs. Reindex uses the stored capture time, never the current clock.
            observed = c.execute('SELECT observed_at FROM captures WHERE id=?', (cid,)).fetchone()[0]
            event = {**event, 'event_key': event['event_key'] + ':observation:' + str(cid),
                     'started_at': observed}
        event={**event,'normalizer_version':1}
        payload=canonical(event)
        previous=c.execute('SELECT payload_json FROM events WHERE source_id=? AND event_key=? ORDER BY capture_id DESC LIMIT 1',
                           (sid,event['event_key'])).fetchone()
        if previous and previous[0]==payload: continue
        eid=c.execute('INSERT INTO events(source_id,event_key,capture_id,kind,started_at,ended_at,url,title,domain,app_id,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
          (sid,event['event_key'],cid,event['kind'],event.get('started_at'),event.get('ended_at'),event.get('url'),event.get('title'),event.get('domain'),event.get('app_id'),payload)).lastrowid
        text=' '.join(str(event.get(k) or '') for k in ('title','url','domain','app_id','kind'))
        c.execute('INSERT INTO event_search(rowid,text) VALUES(?,?)',(eid,text))
    return warnings


def capture(source, archive):
    protect(archive,[source.path])
    # Acquire the writer lock before taking the snapshot: observations cannot commit out of order.
    from .acquisition import acquire
    with writer(archive) as c, acquire(source) as (snap, acquisition):
        sid,identity=source_identity(source,snap)
        register_source(c,source,sid,identity, fidelity='metadata_only' if source.adapter == 'safari_tabs' else 'raw_database')
        schema=[dict(r) for r in snap.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        best_effort = acquisition['consistency'] == 'best_effort'
        if best_effort: schema.append({'type': 'acquisition', 'metadata': acquisition})
        cid=c.execute('INSERT INTO captures(source_id,observed_at,schema_json,collected_by) VALUES(?,?,?,?)',(sid,now(),canonical(schema),c.execute('SELECT archive_id FROM archive_info').fetchone()[0])).lastrowid
        if best_effort: c.execute("UPDATE captures SET provenance='native_best_effort_copy' WHERE id=?", (cid,))
        before={(r['table_name'],r['row_key']):r['content_hash'] for r in c.execute(
            'SELECT cr.table_name,cr.row_key,r.content_hash FROM current_rows cr JOIN records r USING(version_id) WHERE cr.source_id=?',(sid,))}
        seen=set(); changed=0
        for table in schema:
            if table['type']!='table': continue
            for key,payload in snapshot_rows(snap,table['name'],c,omit_blobs=True) if source.adapter == 'safari_tabs' else snapshot_rows(snap,table['name'],c):
                pair=(table['name'],key)
                if pair in seen: raise ValueError('Duplicate row identity in snapshot')
                seen.add(pair)
                if before.get(pair)==digest(payload): continue
                observe(c,sid,cid,*pair,payload); changed+=1
        gone=set() if best_effort else before.keys()-seen
        for table,key in gone:
            c.execute('INSERT INTO changes VALUES(?,?,?,NULL)',(cid,table,key))
            c.execute('DELETE FROM current_rows WHERE source_id=? AND table_name=? AND row_key=?',(sid,table,key))
        c.execute('UPDATE captures SET row_count=?,changed_count=?,deleted_count=? WHERE id=?',(len(seen),changed,len(gone),cid))
        warnings=project(c,source,sid,cid)
        c.execute('INSERT INTO attempts(source_path,attempted_at,status,message) VALUES(?,?,?,?)',
                  (str(source.path),now(),'unsupported_schema' if warnings else 'ok',canonical(warnings)))
    return {'capture_id':cid,'source_id':sid,'rows':len(seen),'changed':changed,'deleted':len(gone),'warnings':warnings,'acquisition':acquisition}


def collect(archive, sources, issues=()):
    results=[]; failures=list(issues)
    sources=list(sources)
    if not sources and not failures:failures.append({'status':'no_sources','message':'No history databases discovered; check discovery paths, exclusions, and permissions'})
    for source in sources:
        try: results.append(capture(source,archive))
        except (OSError,sqlite3.Error,ValueError,TimeoutError) as e:
            status='permission_denied' if isinstance(e,PermissionError) else 'capture_failed'
            failures.append({'path':str(source.path),'status':status,'message':str(e)})
    if failures:
        with writer(archive) as c:
            for f in failures:
                c.execute('INSERT INTO attempts(source_path,attempted_at,status,message) VALUES(?,?,?,?)',
                          (str(f.get('path','')),now(),f.get('status','discovery_failed'),str(f.get('message',f))))
    return {'captures':results,'failures':failures}


def reindex(archive):
    from .adapters import Source
    with writer(archive) as c:
        c.execute('DELETE FROM events'); c.execute('DELETE FROM event_search')
        c.execute('UPDATE archive_info SET index_generation=index_generation+1')
        states={}; warnings=[]
        for cap in c.execute('SELECT * FROM captures ORDER BY id').fetchall():
            sid=cap['source_id']; state=states.setdefault(sid,{})
            for r in c.execute('SELECT ch.*,r.payload_json FROM changes ch LEFT JOIN records r USING(version_id) WHERE capture_id=?',(cap['id'],)):
                pair=(r['table_name'],r['row_key'])
                if r['version_id'] is None: state.pop(pair,None)
                else: state[pair]={'row_key':r['row_key'],'version_id':r['version_id'],'payload':json.loads(r['payload_json'])}
            src=c.execute('SELECT * FROM sources WHERE id=?',(sid,)).fetchone()
            source=Source(path=Path(src['path']),adapter=src['adapter'],browser=src['browser'],profile=src['profile'],device=src['device'])
            rows={t['name']:[] for t in json.loads(cap['schema_json']) if t.get('type')=='table'}
            for (table,_),r in state.items(): rows.setdefault(table,[]).append(r)
            warnings.extend(project(c,source,sid,cap['id'],rows))
        count=c.execute('SELECT count(*) FROM events').fetchone()[0]
    return {'event_versions':count,'warnings':sorted(set(warnings))}

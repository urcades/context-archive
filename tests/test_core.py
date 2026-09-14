import json
import sqlite3
from pathlib import Path
import pytest
from context_archive import core


def db(path, script):
    c = sqlite3.connect(path)
    c.executescript(script)
    c.close()
    return path


def source(path, **kw):
    from context_archive.adapters import Source
    return Source(path=path, adapter=kw.pop('adapter', 'knowledge'), **kw)


def test_reversions_and_deletion_keep_original_values(tmp_path):
    p = db(tmp_path/'source', "CREATE TABLE t(id INTEGER PRIMARY KEY, v, b); INSERT INTO t VALUES(1, 9223372036854775807, x'00ff');")
    a = tmp_path/'archive'
    core.capture(source(p), a)
    with sqlite3.connect(p) as c: c.execute("UPDATE t SET v='B'")
    core.capture(source(p), a)
    with sqlite3.connect(p) as c: c.execute('UPDATE t SET v=9223372036854775807')
    core.capture(source(p), a)
    with sqlite3.connect(p) as c: c.execute('DELETE FROM t')
    core.capture(source(p), a)
    with core.readonly(a) as c:
        assert c.execute('SELECT count(*) FROM records').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM changes').fetchone()[0] == 4
        assert c.execute('SELECT count(*) FROM current_rows').fetchone()[0] == 0
        p = json.loads(c.execute('SELECT payload_json FROM records ORDER BY version_id').fetchone()[0])
        assert p['v'] == {'$integer':'9223372036854775807'}
        assert core.decode_value(p['b'], c) == b'\x00\xff'


def test_wal_commits_and_all_tables_preserved(tmp_path):
    p = tmp_path/'source'; a = tmp_path/'archive'
    with sqlite3.connect(p) as live:
        live.execute('PRAGMA journal_mode=WAL')
        live.executescript("CREATE TABLE x(a TEXT); INSERT INTO x VALUES(CAST(x'ff' AS TEXT)); CREATE TABLE y(k TEXT PRIMARY KEY, v) WITHOUT ROWID; INSERT INTO y VALUES('a', 3.0);")
        core.capture(source(p), a)
    with core.readonly(a) as c:
        assert c.execute('SELECT count(*) FROM records').fetchone()[0] == 2
        row=json.loads(c.execute("SELECT payload_json FROM records WHERE table_name='x'").fetchone()[0])
        assert row['a'] == {'$text_bytes':'/w=='}


def test_failed_snapshot_does_not_tombstone(tmp_path, monkeypatch):
    p=db(tmp_path/'source', 'CREATE TABLE x(a); INSERT INTO x VALUES(1)'); a=tmp_path/'archive'
    core.capture(source(p), a)
    def fail(*args, **kwargs): raise PermissionError('denied')
    monkeypatch.setattr(core, 'snapshot', fail)
    result=core.collect(a, [source(p)])
    assert result['failures'][0]['status']=='permission_denied'
    with core.readonly(a) as c:
        assert c.execute('SELECT count(*) FROM current_rows').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM captures').fetchone()[0]==1


def test_transaction_rolls_back_rows_blobs_and_capture(tmp_path, monkeypatch):
    p=db(tmp_path/'source', "CREATE TABLE x(a); INSERT INTO x VALUES(x'ff')"); a=tmp_path/'archive'
    original=core.snapshot_rows
    def fail(*args):
        yield from original(*args)
        raise ValueError('interrupted')
    monkeypatch.setattr(core,'snapshot_rows', fail)
    with pytest.raises(ValueError, match='interrupted'): core.capture(source(p), a)
    with core.readonly(a) as c:
        for table in ('captures','records','blobs','sources'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def test_source_protection_and_lock(tmp_path):
    import fcntl
    p=db(tmp_path/'source', 'CREATE TABLE x(a)'); a=tmp_path/'archive'
    with pytest.raises(ValueError): core.capture(source(p), p)
    core.initialize(a)
    with open(str(a)+'.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError): core.capture(source(p),a)


def test_store_replacement_creates_new_source(tmp_path):
    p=db(tmp_path/'source', "CREATE TABLE Z_METADATA(Z_UUID); INSERT INTO Z_METADATA VALUES('first');"); a=tmp_path/'archive'
    core.capture(source(p),a)
    with sqlite3.connect(p) as c: c.execute("UPDATE Z_METADATA SET Z_UUID='second'")
    core.capture(source(p),a)
    with core.readonly(a) as c: assert c.execute('SELECT count(*) FROM sources').fetchone()[0]==2


def test_invalid_store_ids_do_not_merge_and_source_aliases_protected(tmp_path):
    p=db(tmp_path/'one', 'CREATE TABLE Z_METADATA(Z_UUID); INSERT INTO Z_METADATA VALUES(NULL);')
    q=db(tmp_path/'two', 'CREATE TABLE Z_METADATA(Z_UUID); INSERT INTO Z_METADATA VALUES(NULL);')
    a=tmp_path/'archive';core.capture(source(p),a);core.capture(source(q),a)
    with core.readonly(a) as c: assert c.execute('SELECT count(*) FROM sources').fetchone()[0]==2


def test_browser_hardlink_identity_stable(tmp_path):
    p=db(tmp_path/'one', 'CREATE TABLE visits(id INTEGER PRIMARY KEY,visit_time,url); CREATE TABLE urls(id INTEGER PRIMARY KEY,url);')
    q=tmp_path/'alias';q.hardlink_to(p);a=tmp_path/'archive'
    core.capture(source(p,adapter='chromium'),a);core.capture(source(q,adapter='chromium'),a)
    with core.readonly(a) as c:assert c.execute('SELECT count(*) FROM sources').fetchone()[0]==1


def test_empty_discovery_is_explicit(tmp_path):
    a=tmp_path/'archive';result=core.collect(a,[])
    assert result['failures'][0]['status']=='no_sources'
    with core.readonly(a) as c:assert c.execute('SELECT count(*) FROM attempts').fetchone()[0]==1


def test_snapshot_reports_lock_separately_from_slow_backup(tmp_path):
    path = db(tmp_path/'locked.sqlite3', 'CREATE TABLE t(value);')
    with sqlite3.connect(path) as blocker:
        blocker.execute('BEGIN EXCLUSIVE')
        with pytest.raises(TimeoutError, match='locked'):
            with core.snapshot(path, timeout=0.01):
                pytest.fail('An exclusively locked database cannot be snapshotted')

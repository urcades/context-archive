import sqlite3
import pytest
from context_archive import core, acquisition, query
from context_archive.adapters import Source


def fixture(tmp_path):
    p=tmp_path/'History'
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT); CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER); INSERT INTO urls VALUES(1,'https://example.org','Title'); INSERT INTO visits VALUES(1,1,13000000000000000);")
    return p


def test_locked_copy_no_deletions_and_reindex(tmp_path,monkeypatch):
    p=fixture(tmp_path);a=tmp_path/'archive';s=Source(p,'chromium','Dia','Test')
    core.capture(s,a)
    with sqlite3.connect(p) as c:c.execute('delete from visits')
    real=core.snapshot
    monkeypatch.setattr(core,'snapshot',lambda path,**kw:real(path,timeout=.01))
    with sqlite3.connect(p) as blocker:
        blocker.execute('BEGIN EXCLUSIVE')
        result=core.capture(s,a)
    assert result['acquisition']['consistency']=='best_effort'
    assert result['deleted']==0
    event=query.timeline(a)['events'][0]
    assert event['acquisition']['consistency']=='best_effort'
    assert event['provenance']=='native_best_effort_copy'
    with core.readonly(a) as c:
        assert c.execute("select count(*) from current_rows where table_name='visits'").fetchone()[0]==1
        assert c.execute('select provenance from captures order by id desc limit 1').fetchone()[0]=='native_best_effort_copy'
        before=c.execute('select payload_json from events order by id').fetchall()
    core.reindex(a)
    with core.readonly(a) as c:assert before==c.execute('select payload_json from events order by id').fetchall()


def test_changed_copy_rejected(tmp_path,monkeypatch):
    p=fixture(tmp_path); real=acquisition.manifest;counter=iter(range(10))
    # Mutate a timestamp instead of introducing a nonexistent sidecar.
    def changed(path):
        d=real(path);d['']=(*d[''][:-1],next(counter));return d
    monkeypatch.setattr(acquisition,'manifest',changed)
    with pytest.raises(acquisition.SourceChangedError):
        with acquisition.copied_snapshot(p):pass


def test_wal_replayed(tmp_path):
    p=fixture(tmp_path)
    with sqlite3.connect(p) as live:
        live.execute('pragma journal_mode=WAL');live.execute('insert into visits values(2,1,13000000000000001)');live.commit()
        with acquisition.copied_snapshot(p) as (snap,meta):
            assert snap.execute('select count(*) from visits').fetchone()[0]==2
            assert '-wal' in meta['copied_suffixes']


def test_corrupt_copy_rejected(tmp_path):
    p=tmp_path/'bad';p.write_bytes(b'not sqlite')
    with pytest.raises(sqlite3.DatabaseError):
        with acquisition.copied_snapshot(p):pass


def test_hot_journal_recovered_only_in_copy(tmp_path):
    p=tmp_path/'History'
    live=sqlite3.connect(p)
    try:
        live.execute('create table t(id integer primary key,value text)')
        live.executemany('insert into t(value) values(?)',[('original'*1000,)]*50);live.commit()
        live.execute('pragma cache_size=1');live.execute('begin exclusive')
        live.execute("update t set value='uncommitted'")
        with acquisition.copied_snapshot(p) as (snap,meta):
            assert snap.execute('select value from t limit 1').fetchone()[0]=='original'*1000
            assert '-journal' in meta['copied_suffixes']
        assert live.execute('select value from t limit 1').fetchone()[0]=='uncommitted'
        live.rollback()
    finally:live.close()


def test_permission_failure_never_falls_back(tmp_path,monkeypatch):
    p=fixture(tmp_path)
    def denied(*a,**k):raise PermissionError('denied')
    def forbidden(*a,**k):pytest.fail('Permission errors must not trigger copying')
    monkeypatch.setattr(core,'snapshot',denied)
    monkeypatch.setattr(acquisition,'copied_snapshot',forbidden)
    with pytest.raises(PermissionError):
        with acquisition.acquire(Source(p,'chromium')):pass

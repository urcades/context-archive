import base64
import json
import sqlite3
import pytest
from context_archive import core, transfer
from context_archive.adapters import Source


def fixture(tmp_path):
    p=tmp_path/'source';a=tmp_path/'archive'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE t(id INTEGER PRIMARY KEY,b,large)')
        c.execute('INSERT INTO t VALUES(1,?,?)',(b'\xff\x00',2**63-1))
    core.capture(Source(p,'knowledge'),a)
    return p,a


def test_export_is_complete_self_contained_and_backup_matches(tmp_path):
    p,a=fixture(tmp_path); out=tmp_path/'export.jsonl'
    transfer.export_archive(a,out)
    lines=[json.loads(x) for x in out.read_text().splitlines()]
    assert lines[0]['type']=='manifest'
    assert lines[0]['format']=='context-archive'
    assert lines[-1]['type']=='end'
    assert lines[-1]['preceding_lines']==len(lines)-1
    blob=next(x['record'] for x in lines if x['type']=='blobs')
    assert base64.b64decode(blob['data']['$bytes_base64'])==b'\xff\x00'
    record=next(x['record'] for x in lines if x['type']=='records')
    assert json.loads(record['payload_json'])['large']=={'$integer':str(2**63-1)}
    dest=tmp_path/'backup'
    transfer.backup(a,dest)
    with core.readonly(a) as c,core.readonly(dest) as d:
        assert c.execute('SELECT * FROM records').fetchall()==d.execute('SELECT * FROM records').fetchall()
        assert d.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_export_backup_protect_sources_and_sidecars(tmp_path):
    p,a=fixture(tmp_path)
    for action in (transfer.export_archive,transfer.backup):
        for dest in (p,a,tmp_path/'source-wal',tmp_path/'archive.lock'):
            with pytest.raises(ValueError):action(a,dest)
        alias=tmp_path/'alias'
        if not alias.exists():alias.hardlink_to(p)
        with pytest.raises(ValueError):action(a,alias)


def test_failed_export_keeps_old_destination(tmp_path,monkeypatch):
    p,a=fixture(tmp_path);dest=tmp_path/'out';dest.write_text('old')
    def fail(*a):raise ValueError('bad export')
    monkeypatch.setattr(transfer,'encode_cell',fail)
    with pytest.raises(ValueError):transfer.export_archive(a,dest)
    assert dest.read_text()=='old'


def test_import_inputs_and_moved_native_paths_protected(tmp_path):
    from context_archive import importers
    legacy=tmp_path/'legacy.jsonl';legacy.write_text('not json\n');a=tmp_path/'archive'
    importers.import_browser(legacy,a)
    with pytest.raises(ValueError):transfer.export_archive(a,legacy)
    p=tmp_path/'oldpath'
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE Z_METADATA(Z_UUID);INSERT INTO Z_METADATA VALUES('stable');")
    core.capture(Source(p,'knowledge'),a)
    new=tmp_path/'newpath';p.rename(new)
    core.capture(Source(new,'knowledge'),a)
    for path in (p,new):
        with pytest.raises(ValueError):transfer.export_archive(a,path)

import json
from pathlib import Path
from context_archive import control, adapters


def test_status_missing_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, 'discover', lambda **kw: {'sources': [], 'issues': []})
    result=control.status(tmp_path/'data/a.db', tmp_path/'config.json')
    assert result['archive_exists'] is False
    assert result['latest_cycle'] is None
    assert list(tmp_path.iterdir()) == []


def test_update_preserves_unknown_and_validates(tmp_path):
    config=tmp_path/'config.json'; config.write_text('{"future":42}')
    control.update(config, {'paused':True})
    control.update(config, {'source_path':'/tmp/example','enabled':False})
    assert json.loads(config.read_text()) == {'future':42,'paused':True,'disabled_source_paths':[str(Path('/tmp/example').resolve())]}
    import pytest
    with pytest.raises(ValueError): control.update(config, {'paused':1})
    with pytest.raises(ValueError): control.update(config, {'source_path':'relative','enabled':True})


def test_paused_and_interrupted(tmp_path, monkeypatch):
    config=tmp_path/'config.json'; config.write_text('{"paused":true}')
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[],'issues':[]})
    result=control.cycle(tmp_path/'a.db',config)
    assert result['status']=='paused'
    assert not (tmp_path/'a.db').exists()
    receipt=tmp_path/'control/latest_cycle.json'
    saved=json.loads(receipt.read_text()); saved['status']='running'; saved['ended_at']=None
    receipt.write_text(json.dumps(saved))
    before=receipt.read_bytes()
    assert control.status(tmp_path/'a.db',config)['latest_cycle']['status']=='interrupted'
    assert receipt.read_bytes()==before


def test_overlap(tmp_path,monkeypatch):
    config=tmp_path/'config.json'
    def collect(*args):
        import pytest
        with pytest.raises(RuntimeError,match='already running'): control.cycle(tmp_path/'a.db',config)
        assert control.status(tmp_path/'a.db',config)['collecting']
        return {'captures':[{'capture_id':1}],'failures':[]}
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[],'issues':[]})
    monkeypatch.setattr(control.core,'collect',collect)
    assert control.cycle(tmp_path/'a.db',config)['status']=='completed'


def test_disabled_discovery(tmp_path):
    source=tmp_path/'source.db'; source.touch()
    config={'sources':[{'path':str(source),'adapter':'knowledge'}], 'disabled_source_paths':[str(source)]}
    assert adapters.discover(home=tmp_path,config=config)['sources']==[]
    assert len(adapters.discover(home=tmp_path,config=config,include_disabled=True)['sources'])==1


def test_archived_failure_and_disabled_history(tmp_path,monkeypatch):
    from context_archive import core
    archive=tmp_path/'a.db'; config=tmp_path/'config.json'
    with core.writer(archive) as db:
        db.execute("INSERT INTO attempts(source_path,attempted_at,status,message) VALUES('/missing','2026-01-01T00:00:00Z','capture_failed','database is locked')")
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[],'issues':[]})
    before=archive.read_bytes()
    assert control.status(archive,config)['sources'][0]['status']=='locked'
    assert archive.read_bytes()==before
    control.update(config,{'source_path':'/missing','enabled':False})
    assert control.status(archive,config)['sources'][0]['status']=='disabled'


def test_partial_and_exception_receipts(tmp_path,monkeypatch):
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[],'issues':[]})
    monkeypatch.setattr(control.core,'collect',lambda *args:{'captures':[{'capture_id':1}], 'failures':[{'status':'permission_denied','message':'denied'}]})
    config=tmp_path/'config.json'; archive=tmp_path/'a.db'
    assert control.cycle(archive,config,'background')['status']=='partial'
    def explode(*args): raise ValueError('broken')
    monkeypatch.setattr(control.core,'collect',explode)
    result=control.cycle(archive,config)
    assert result['status']=='failed' and result['ended_at']
    assert control.status(archive,config)['latest_cycle']==result


def test_success_is_based_on_capture_and_poll_does_not_snapshot(tmp_path,monkeypatch):
    import sqlite3
    source=tmp_path/'source.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE ZOBJECT(ZUUID TEXT,ZSTREAMNAME TEXT,ZSTARTDATE REAL,ZENDDATE REAL)')
    archive=tmp_path/'archive.db'; config=tmp_path/'config.json'
    config.write_text(json.dumps({'sources':[{'path':str(source),'adapter':'knowledge'}]}))
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[adapters.Source(source,'knowledge')],'issues':[]})
    assert control.cycle(archive,config)['status']=='completed'
    def forbidden(*args,**kwargs): raise AssertionError('Status must not snapshot sources')
    monkeypatch.setattr(control.core,'snapshot',forbidden)
    result=control.status(archive,config)
    assert result['sources'][0]['status']=='ready'
    assert result['last_success_at']


def test_interruption_persists_and_releases_lock(tmp_path,monkeypatch):
    import pytest
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[],'issues':[]})
    def interrupt(*args): raise KeyboardInterrupt()
    monkeypatch.setattr(control.core,'collect',interrupt)
    with pytest.raises(KeyboardInterrupt): control.cycle(tmp_path/'a.db',tmp_path/'config.json')
    result=control.status(tmp_path/'a.db',tmp_path/'config.json')
    assert not result['collecting']
    assert result['latest_cycle']['status']=='interrupted'
    assert result['latest_cycle']['ended_at']


def test_private_control_permissions(tmp_path,monkeypatch):
    import os
    config=tmp_path/'config.json'
    previous=os.umask(0o022)
    try: control.update(config,{'paused':True})
    finally: os.umask(previous)
    assert config.stat().st_mode & 0o777 == 0o600
    assert (tmp_path/'control').stat().st_mode & 0o777 == 0o700
    assert (tmp_path/'control/config.lock').stat().st_mode & 0o777 == 0o600


def test_completed_source_survives_later_interruption(tmp_path,monkeypatch):
    import pytest
    first=adapters.Source(tmp_path/'first','knowledge'); second=adapters.Source(tmp_path/'second','knowledge')
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[first,second],'issues':[]})
    def collect(archive,sources,issues):
        if sources[0]==second:
            receipt=json.loads((tmp_path/'control/latest_cycle.json').read_text())
            assert receipt['captures']==[{'capture_id':1}]
            raise KeyboardInterrupt()
        return {'captures':[{'capture_id':1}],'failures':[]}
    monkeypatch.setattr(control.core,'collect',collect)
    with pytest.raises(KeyboardInterrupt): control.cycle(tmp_path/'a.db',tmp_path/'config.json')
    assert control.status(tmp_path/'a.db',tmp_path/'config.json')['latest_cycle']['captures']==[{'capture_id':1}]


def test_malformed_explicit_paths_return_discovery_issues(tmp_path,monkeypatch):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'sources':[{'path':None,'adapter':'knowledge'},{'path':123,'adapter':'knowledge'}]}))
    original=adapters.discover
    monkeypatch.setattr(adapters,'discover',lambda **kw:original(home=tmp_path,**kw))
    result=control.status(tmp_path/'a.db',config)
    assert not result['sources']
    assert len(result['issues'])==2
    assert all(issue['status']=='discovery_failed' for issue in result['issues'])


def test_status_sees_committed_wal_evidence(tmp_path,monkeypatch):
    import sqlite3
    archive=tmp_path/'a.db'
    with control.core.writer(archive): pass
    monkeypatch.setattr(adapters,'discover',lambda **kw:{'sources':[],'issues':[]})
    with sqlite3.connect(archive) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO attempts(source_path,attempted_at,status,message) VALUES('/wal-source','2026-01-01T00:00:00Z','permission_denied','denied')")
        db.commit()
        assert Path(str(archive)+'-wal').stat().st_size > 0
        result=control.status(archive,tmp_path/'config.json')
        assert result['sources'][0]['path']=='/wal-source'
        assert result['sources'][0]['status']=='permission_denied'


def test_invalid_metadata_keeps_status_native_decodable(tmp_path,monkeypatch):
    bad=tmp_path/'bad.db'; bad.touch()
    good=tmp_path/'good.db'; good.touch()
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'sources':[
        {'path':str(bad),'adapter':'knowledge','browser':123,'profile':['bad']},
        {'path':str(good),'adapter':'knowledge','browser':'Example','profile':'Default'},
        {'path':str(tmp_path/'missing'),'adapter':123}]}))
    original=adapters.discover
    monkeypatch.setattr(adapters,'discover',lambda **kw:original(home=tmp_path,**kw))
    result=control.status(tmp_path/'a.db',config)
    assert any(s['path']==str(good) for s in result['sources'])
    assert len(result['issues'])==2
    assert all(i['status']=='discovery_failed' for i in result['issues'])
    for source in result['sources']:
        for field in ('adapter','browser','profile'):
            assert source[field] is None or isinstance(source[field],str)

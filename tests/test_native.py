import json
import plistlib
import subprocess
from pathlib import Path
import pytest
from context_archive import core, native


class Services:
    def __init__(self, loaded=True):
        self.loaded=loaded; self.native_loaded=False; self.calls=[]; self.fail_stop=False
    def __call__(self,args,**kwargs):
        self.calls.append(args)
        action=args[1]
        if action=='print':
            loaded=self.native_loaded if native.LABEL in args[-1] else self.loaded
            return subprocess.CompletedProcess(args,0 if loaded else 1,'state = not running' if loaded else '', '')
        if action=='bootout':
            if self.fail_stop:return subprocess.CompletedProcess(args,1,'','failed')
            self.loaded=False
        if action=='bootstrap':self.loaded=True
        return subprocess.CompletedProcess(args,0,'','')


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    archive=tmp_path/'state/data/archive.sqlite3';core.initialize(archive)
    config=tmp_path/'state/config.json';config.write_text('{"custom":42}')
    plist=tmp_path/'Library/LaunchAgents/local.context-archive.plist';plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({'Label':'local.context-archive','ProgramArguments':['/example/python','-m','context_archive.cli','capture']}))
    services=Services();monkeypatch.setattr(native.subprocess,'run',services)
    return archive,config,plist,services


def test_migration_backup_and_rollback_preserve_current_archive(tmp_path,monkeypatch):
    archive,config,plist,services=setup(tmp_path,monkeypatch)
    identity=core.readonly(archive).execute('SELECT archive_id FROM archive_info').fetchone()[0]
    result=native.prepare_migration(archive,config)
    assert Path(result['backup_path']).exists() and not services.loaded
    assert not plist.exists()
    assert json.loads(config.read_text())=={'custom':42}
    with core.writer(archive) as c:c.execute("INSERT INTO imports VALUES('test','test','now','{}')")
    native.rollback(archive,config)
    assert services.loaded and plist.exists()
    with core.readonly(archive) as c:
        assert c.execute('SELECT archive_id FROM archive_info').fetchone()[0]==identity
        assert c.execute('SELECT count(*) FROM imports').fetchone()[0]==1


def test_migration_refuses_native_loaded_and_other_plist(tmp_path,monkeypatch):
    archive,config,plist,services=setup(tmp_path,monkeypatch)
    services.native_loaded=True
    with pytest.raises(RuntimeError):native.prepare_migration(archive,config)
    services.native_loaded=False;plist.write_bytes(plistlib.dumps({'Label':'unrelated'}))
    with pytest.raises(ValueError):native.prepare_migration(archive,config)
    assert services.loaded


def test_failed_stop_preserves_backup_and_rollback_requires_native_stopped(tmp_path,monkeypatch):
    archive,config,plist,services=setup(tmp_path,monkeypatch)
    services.fail_stop=True
    with pytest.raises(RuntimeError):native.prepare_migration(archive,config)
    assert services.loaded
    manifest=json.loads((config.parent/'control/migration.json').read_text())
    assert Path(manifest['backup_path']).exists()
    services.native_loaded=True
    with pytest.raises(RuntimeError):native.rollback(archive,config)


def test_migration_retry_retains_original_backup(tmp_path,monkeypatch):
    archive,config,plist,services=setup(tmp_path,monkeypatch)
    first=native.prepare_migration(archive,config)
    second=native.prepare_migration(archive,config)
    assert first==second and second['legacy_loaded'] is True
    native.rollback(archive,config)
    assert plist.exists() and services.loaded


@pytest.mark.parametrize('interruption',['bootout','unlink'])
@pytest.mark.parametrize('retry',[False,True])
def test_interrupted_migration_keeps_original_service_for_rollback(tmp_path,monkeypatch,interruption,retry):
    archive,config,plist,services=setup(tmp_path,monkeypatch)
    original=plist.read_bytes()
    with monkeypatch.context() as patch:
        if interruption=='bootout':
            def interrupted_unlink(path,*args,**kwargs):
                if path==plist:raise RuntimeError('interrupted after bootout')
                return original_unlink(path,*args,**kwargs)
            original_unlink=Path.unlink
            patch.setattr(Path,'unlink',interrupted_unlink)
        else:
            original_write=native._write_json
            def interrupted_write(path,value):
                if value.get('state')=='prepared':raise RuntimeError('interrupted after unlink')
                return original_write(path,value)
            patch.setattr(native,'_write_json',interrupted_write)
        with pytest.raises(RuntimeError):native.prepare_migration(archive,config)
    before=json.loads((config.parent/'control/migration.json').read_text())
    assert before['state']=='preparing' and before['legacy_loaded']
    assert not services.loaded
    if retry:
        after=native.prepare_migration(archive,config)
        assert {k:v for k,v in after.items() if k!='state'}=={k:v for k,v in before.items() if k!='state'}
    native.rollback(archive,config)
    assert services.loaded and plist.read_bytes()==original


def test_stop_failed_retry_preserves_original_snapshot(tmp_path,monkeypatch):
    archive,config,plist,services=setup(tmp_path,monkeypatch)
    original=plist.read_bytes();services.fail_stop=True
    with pytest.raises(RuntimeError):native.prepare_migration(archive,config)
    before=json.loads((config.parent/'control/migration.json').read_text())
    services.fail_stop=False
    after=native.prepare_migration(archive,config)
    assert {k:v for k,v in after.items() if k!='state'}=={k:v for k,v in before.items() if k!='state'}
    native.rollback(archive,config)
    assert services.loaded and plist.read_bytes()==original


def test_prepared_migration_rejects_different_archive(tmp_path,monkeypatch):
    archive,config,plist,services=setup(tmp_path,monkeypatch)
    original=native.prepare_migration(archive,config)
    with pytest.raises(ValueError,match='another archive'):
        native.prepare_migration(tmp_path/'other.sqlite3',config)
    assert json.loads((config.parent/'control/migration.json').read_text())==original

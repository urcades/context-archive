"""Explicit native-app adoption with local rollback material and no archive rewind."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time
import uuid
from . import core, runtime, transfer

LABEL='io.github.urcades.ContextArchive.agent'


def _service(action, *args):
    return subprocess.run(['/bin/launchctl',action,*args],capture_output=True,text=True,timeout=10)


def _target(label):
    return f'gui/{os.getuid()}/{label}'


def _loaded(label):
    return _service('print',_target(label)).returncode==0


def _write_json(path,value):
    with transfer.atomic_file(path) as f:f.write(core.canonical(value)+'\n')


def _copy(source,destination):
    with transfer.atomic_file(destination,binary=True) as f:f.write(source.read_bytes())


@contextmanager
def _migration_lock(root):
    directory=root/'control';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(directory/'migration.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as e:raise RuntimeError('Another migration is in progress') from e
        yield


@contextmanager
def _idle_archive(archive,timeout=120):
    """Wait for the old job, then hold the same lock used by every archive writer.

    No SQLite transaction is opened here. Backup uses SQLite's backup API; holding
    the lock prevents a new capture from changing evidence during service handoff.
    """
    deadline=time.monotonic()+timeout
    while True:
        status=_service('print',_target(runtime.LABEL))
        active=status.returncode==0 and re.search(r'^\s*pid\s*=\s*\d+',status.stdout,re.M)
        if not active:break
        if time.monotonic()>=deadline:raise TimeoutError('Current collector is still active; retry adoption after it finishes')
        time.sleep(.25)
    archive.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(str(archive)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as f:
        while True:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise TimeoutError('Archive writer is still active; retry later')
                time.sleep(.25)
        yield


def prepare_migration(archive,config):
    archive=Path(archive).resolve();config=Path(config).resolve();root=config.parent
    with _migration_lock(root):
        if _loaded(LABEL):raise RuntimeError('The native agent is already registered; use its existing controls')
        plist=runtime.agent_path()
        if plist.exists() and plistlib.loads(plist.read_bytes()).get('Label')!=runtime.LABEL:
            raise ValueError('Refusing unrelated LaunchAgent at the legacy path')
        manifest_path=root/'control/migration.json'
        # Keep the original rollback snapshot across every incomplete handoff.
        # Side effects may finish even when the final manifest write does not.
        manifest=None
        if manifest_path.exists():
            previous=json.loads(manifest_path.read_text())
            if previous.get('archive_path')!=str(archive) or previous.get('migration_path')!=str(manifest_path):
                raise ValueError('Migration belongs to another archive or configuration root')
            if previous.get('state') in ('preparing','stop_failed','prepared'):
                if previous.get('backup_path') and not Path(previous['backup_path']).is_file():
                    raise ValueError('Migration archive backup is missing')
                saved=Path(previous['saved_plist'])
                if previous['legacy_plist_existed'] and plistlib.loads(saved.read_bytes()).get('Label')!=runtime.LABEL:
                    raise ValueError('Invalid saved LaunchAgent')
                manifest=previous
                if manifest['state']=='prepared':return manifest
        with _idle_archive(archive):
            if manifest is None:
                directory=root/'migration-backups'/str(uuid.uuid4())
                directory.mkdir(parents=True,mode=0o700)
                backup_path=directory/'archive.sqlite3'
                if archive.exists():transfer.backup(archive,backup_path)
                saved_plist=directory/'legacy.plist'
                if plist.exists():_copy(plist,saved_plist)
                if config.exists():_copy(config,directory/'config.json')
                codex=Path.home()/'.codex/config.toml'
                if codex.exists():_copy(codex,directory/'codex-config.toml')
                manifest={'schema_version':1,'state':'preparing','created_at':core.now(),
                          'migration_path':str(manifest_path),'archive_path':str(archive),
                          'backup_path':str(backup_path) if archive.exists() else None,
                          'legacy_loaded':_loaded(runtime.LABEL),'legacy_plist_existed':plist.exists(),
                          'saved_plist':str(saved_plist),'backup_directory':str(directory),
                          'note':'Rollback restores service configuration, never archived evidence. Codex configuration snapshot is retained for selective manual recovery.'}
                _write_json(manifest_path,manifest)
            if _loaded(runtime.LABEL):
                result=_service('bootout',_target(runtime.LABEL))
                if result.returncode and _loaded(runtime.LABEL):
                    manifest['state']='stop_failed';_write_json(manifest_path,manifest)
                    raise RuntimeError('Could not stop previous collector; backup retained and adoption aborted')
            # Remove only our saved legacy plist so login cannot start a second writer.
            # Rollback restores its exact bytes; bootout alone lasts only this login.
            if manifest['legacy_plist_existed']:plist.unlink(missing_ok=True)
            manifest['state']='prepared';_write_json(manifest_path,manifest)
            return manifest


def rollback(archive,config):
    archive=Path(archive).resolve();root=Path(config).resolve().parent
    with _migration_lock(root):
        if _loaded(LABEL):raise RuntimeError('Unregister the native background agent before restoring the previous collector')
        manifest_path=root/'control/migration.json'
        if not manifest_path.exists():raise ValueError('No previous collector configuration has been saved')
        manifest=json.loads(manifest_path.read_text())
        if manifest.get('archive_path')!=str(archive) or manifest.get('migration_path')!=str(manifest_path):
            raise ValueError('Rollback belongs to another archive or configuration root')
        saved=Path(manifest['saved_plist']);plist=runtime.agent_path()
        if manifest['legacy_plist_existed']:
            if plistlib.loads(saved.read_bytes()).get('Label')!=runtime.LABEL:raise ValueError('Invalid saved LaunchAgent')
            if plist.exists() and plistlib.loads(plist.read_bytes()).get('Label')!=runtime.LABEL:raise ValueError('Refusing to replace unrelated LaunchAgent')
            _copy(saved,plist)
        if manifest['legacy_loaded'] and not _loaded(runtime.LABEL):
            result=_service('bootstrap',f'gui/{os.getuid()}',str(plist))
            if result.returncode:raise RuntimeError('Could not restore previous collector: '+result.stderr[-2000:])
        manifest['state']='rolled_back';manifest['rolled_back_at']=core.now();_write_json(manifest_path,manifest)
        return {'restored':True,'legacy_loaded':_loaded(runtime.LABEL),'archive_preserved':True,'migration_path':str(manifest_path)}

"""Local control plane with logically read-only polling of archive evidence.

Polls never snapshot sources or modify archive rows/configuration. SQLite may
manage transient WAL sidecars while reading committed live archive evidence.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import uuid
from . import adapters, core


def read_config(path):
    value=json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value,dict): raise ValueError('Config must be an object')
    for key in ('sources','exclusions','disabled_source_paths'):
        if not isinstance(value.get(key,[]),list): raise ValueError(key+' must be an array')
    for key in ('exclusions','disabled_source_paths'):
        if not all(isinstance(x,str) for x in value.get(key,[])): raise ValueError(key+' must contain strings')
    if not all(Path(x).is_absolute() for x in value.get('disabled_source_paths',[])): raise ValueError('Disabled paths must be absolute')
    if not isinstance(value.get('paused',False),bool): raise ValueError('paused must be a boolean')
    return value


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.parent.chmod(0o700)
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,ensure_ascii=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)
        directory_fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temp): os.unlink(temp)


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.parent.chmod(0o700)
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
    os.fchmod(descriptor,0o600)
    with os.fdopen(descriptor,'a+') as stream:
        try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Control operation already running')
        try: yield
        finally: fcntl.flock(stream,fcntl.LOCK_UN)


def collecting(path):
    # Opening only existing locks keeps status genuinely read-only on first run.
    try: stream=path.open('r')
    except FileNotFoundError: return False
    with stream:
        try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(stream,fcntl.LOCK_UN)
    return False


def update(config_path,params):
    if not isinstance(params,dict): raise ValueError('Parameters must be an object')
    if set(params)=={'paused'} and isinstance(params['paused'],bool): pass
    elif (set(params)=={'source_path','enabled'} and isinstance(params['source_path'],str)
          and Path(params['source_path']).is_absolute() and isinstance(params['enabled'],bool)): pass
    else: raise ValueError('Expected paused boolean or absolute source_path and enabled boolean')
    with lock(config_path.parent/'control/config.lock'):
        config=read_config(config_path)
        if 'paused' in params: config['paused']=params['paused']
        else:
            path=str(Path(params['source_path']).resolve())
            disabled={str(Path(p).resolve()) for p in config.get('disabled_source_paths',[])}
            if params['enabled']: disabled.discard(path)
            else: disabled.add(path)
            config['disabled_source_paths']=sorted(disabled)
        atomic_json(config_path,config)
    return {'paused':config.get('paused',False),'disabled_source_paths':config.get('disabled_source_paths',[])}


def failure_status(value,message=''):
    if value in ('permission_denied','discovery_failed','no_sources'): return value
    if 'locked' in message.lower() or 'busy' in message.lower(): return 'locked'
    return 'failed'


def status(archive,config_path):
    config=read_config(config_path); directory=config_path.parent/'control'
    active=collecting(directory/'cycle.lock')
    try: latest=json.loads((directory/'latest_cycle.json').read_text())
    except FileNotFoundError: latest=None
    if latest and latest['status']=='running' and not active:
        latest={**latest,'status':'interrupted','ended_at':None}
    discovered=adapters.discover(config=config,include_disabled=True)
    issues=list(discovered['issues']); sources={}; attempts={}
    disabled={str(Path(p).resolve()) for p in config.get('disabled_source_paths',[])}
    if archive.exists():
        try:
            with closing(core.readonly(archive)) as db:
                deadline=time.monotonic()+5
                db.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
                for row in db.execute('SELECT s.path,s.adapter,s.browser,s.profile,MAX(c.observed_at) AS last_success_at FROM sources s LEFT JOIN captures c ON c.source_id=s.id GROUP BY s.path'):
                    sources[row['path']]=dict(row)
                for row in db.execute('SELECT a.* FROM attempts a JOIN (SELECT MAX(id) AS id FROM attempts GROUP BY source_path) latest ON latest.id=a.id'):
                    attempts[row['source_path']]=dict(row)
        except (OSError,sqlite3.Error) as exc:
            issues.append({'status':failure_status('failed',str(exc)),'path':str(archive),'message':str(exc)})
    for source in discovered['sources']:
        old=sources.get(str(source.path),{})
        sources[str(source.path)]={'path':str(source.path),'adapter':source.adapter,'browser':source.browser,'profile':source.profile,'last_success_at':old.get('last_success_at')}
    for entry in config.get('sources',[]):
        if isinstance(entry,dict) and isinstance(entry.get('path'),str):
            try: path=str(Path(entry['path']).expanduser().resolve())
            except (OSError,ValueError,RuntimeError): continue  # Discovery already reported it.
            sources.setdefault(path,{'path':path,'adapter':entry.get('adapter'),'browser':entry.get('browser'),'profile':entry.get('profile'),'last_success_at':None})
    for path in disabled|set(attempts):
        if path: sources.setdefault(path,{'path':path,'adapter':None,'browser':None,'profile':None,'last_success_at':None})
    for path,source in sources.items():
        # Keep optional native fields decodable even for malformed configuration
        # or historical evidence created before metadata validation existed.
        for field in ('adapter','browser','profile'):
            if source.get(field) is not None and not isinstance(source[field],str): source[field]=None
        source.update(enabled=path not in disabled,status='not_collected',message='No successful capture recorded')
        success=source['last_success_at']; attempt=attempts.get(path)
        if success:
            source.update(status='ready',message='Last successful capture recorded')
            if datetime.fromisoformat(success.replace('Z','+00:00')) < datetime.now(timezone.utc)-timedelta(minutes=15):
                source.update(status='stale',message='Last successful capture is more than 15 minutes old')
        if attempt and attempt['status'] not in ('ok','success') and (not success or attempt['attempted_at']>=success):
            source.update(status=failure_status(attempt['status'],attempt['message']),message=attempt['message'])
        for issue in issues:
            if issue.get('path')==path: source.update(status=failure_status(issue['status'],issue['message']),message=issue['message'])
        if not source['enabled']: source.update(status='disabled',message='Collection disabled')
    return {'schema_version':1,'paused':config.get('paused',False),'collecting':active,'archive_exists':archive.exists(),
            'sources':sorted(sources.values(),key=lambda x:x['path']),'issues':issues,'latest_cycle':latest,
            'last_success_at':max((s['last_success_at'] for s in sources.values() if s['last_success_at']),default=None),
            'permission_executable':str(Path(sys.executable).resolve()),'archive_path':str(archive),'config_path':str(config_path)}


def cycle(archive,config_path,origin='manual'):
    if origin not in ('manual','background'): raise ValueError('Invalid origin')
    directory=config_path.parent/'control'
    with lock(directory/'cycle.lock'):
        receipt={'id':str(uuid.uuid4()),'origin':origin,'status':'running','started_at':core.now(),'ended_at':None,'captures':[],'failures':[]}
        atomic_json(directory/'latest_cycle.json',receipt)
        try:
            config=read_config(config_path)
            if config.get('paused',False): receipt['status']='paused'
            else:
                discovered=adapters.discover(config=config)
                core.protect(archive,[s.path for s in discovered['sources']])
                # Persist every committed source before advancing: a later killed
                # snapshot must not hide earlier successfully archived evidence.
                batches=[([source],[]) for source in discovered['sources']]
                if discovered['issues'] or not batches: batches.append(([],discovered['issues']))
                for sources,issues in batches:
                    result=core.collect(archive,sources,issues)
                    receipt['captures'].extend(result['captures'])
                    receipt['failures'].extend(result['failures'])
                    atomic_json(directory/'latest_cycle.json',receipt)
                for failure in receipt['failures']: failure['status']=failure_status(failure.get('status','failed'),failure.get('message',''))
                receipt['status']=('partial' if receipt['captures'] else 'failed') if receipt['failures'] else 'completed'
        except BaseException as exc:
            receipt['status']='interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed'
            receipt['failures'].append({'status':failure_status('failed',str(exc)),'message':str(exc)})
            if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
        finally:
            receipt['ended_at']=core.now(); atomic_json(directory/'latest_cycle.json',receipt)
        return receipt

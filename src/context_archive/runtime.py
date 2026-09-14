"""Own LaunchAgent lifecycle; archive and runtime data survive uninstall."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL='local.context-archive'


def home():
    value=Path(os.environ.get('CONTEXT_ARCHIVE_HOME',str(Path.home()/'Library/Application Support/ContextArchive'))).expanduser()
    if not value.is_absolute(): raise ValueError('CONTEXT_ARCHIVE_HOME must be absolute')
    return value.resolve()


def agent_path():
    return Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'


def interpreter():
    # Execute the venv entry point (resolving its symlink would lose site-packages).
    return str(Path(sys.executable).absolute())


def background_status():
    if sys.platform!='darwin': return {'loaded':False,'detail':'launchd is available on macOS only'}
    result=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{LABEL}'],capture_output=True,text=True)
    return {'loaded':result.returncode==0,'detail':(result.stdout or result.stderr)[-8000:]}


def install(archive,config,no_start=False,codex=False):
    if no_start and background_status()['loaded']:
        raise RuntimeError('Collector is loaded; run context-archive uninstall before install --no-start. Archive and runtime data are preserved.')
    root=home(); path=agent_path()
    if path.exists() and plistlib.loads(path.read_bytes()).get('Label')!=LABEL:
        raise ValueError('LaunchAgent label does not match')
    logs=root/'logs'; logs.mkdir(parents=True,exist_ok=True,mode=0o700)
    (root/'data').mkdir(parents=True,exist_ok=True,mode=0o700)
    path.parent.mkdir(parents=True,exist_ok=True)
    args=[interpreter(),'-m','context_archive.cli','--archive',str(Path(archive).resolve()),'--config',str(Path(config).resolve()),'capture']
    data={'Label':LABEL,'ProgramArguments':args,'StartInterval':300,'RunAtLoad':not no_start,'Disabled':bool(no_start),
          'StandardOutPath':str(logs/'collector.stdout.log'),'StandardErrorPath':str(logs/'collector.stderr.log'),
          'EnvironmentVariables':{'CONTEXT_ARCHIVE_HOME':str(root)},'Umask':63}
    temporary=path.with_suffix('.plist.tmp'); temporary.write_bytes(plistlib.dumps(data)); temporary.chmod(0o600); temporary.replace(path)
    if not no_start:
        subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],capture_output=True)
        subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
    if codex:
        subprocess.run(['codex','mcp','add','context-archive','--',interpreter(),'-m','context_archive.mcpserver','--archive',str(Path(archive).resolve())],check=True)
    return {'plist':str(path),'started':not no_start,'codex_registered':codex,'permission_executable':str(Path(sys.executable).resolve()),'runtime_executable':interpreter()}


def uninstall():
    path=agent_path()
    if path.exists():
        # Never remove an unrelated file at the expected path.
        if plistlib.loads(path.read_bytes()).get('Label')!=LABEL: raise ValueError('LaunchAgent label does not match')
        result=subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],capture_output=True)
        if result.returncode and background_status()['loaded']:
            raise RuntimeError('Collector remains loaded; LaunchAgent preserved after bootout failure')
        path.unlink()
    return {'removed':str(path),'data_preserved':True,'note':'Explicit Codex registration is preserved; remove it with codex mcp remove context-archive if desired.'}

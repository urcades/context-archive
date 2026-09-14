import json
import plistlib
import pytest
from pathlib import Path
from context_archive import cli, core, runtime


@pytest.fixture(autouse=True)
def no_live_launchctl(monkeypatch):
    monkeypatch.setattr(runtime,'background_status',lambda: {'loaded':False})


def test_cli_query_and_bad_parameters(tmp_path, capsys):
    archive=tmp_path/'archive.sqlite3'; core.initialize(archive)
    assert cli.main(['--archive',str(archive),'sources']) == 0
    assert json.loads(capsys.readouterr().out)['rows'] == []
    assert cli.main(['sources','--archive',str(archive),'--params','[]']) == 2
    assert cli.main(['sources','--archive',str(archive),'--params','{"archive":"other"}']) == 2


def test_no_start_uninstall_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path)); monkeypatch.setenv('CONTEXT_ARCHIVE_HOME',str(tmp_path/'Private Store'))
    calls=[]
    monkeypatch.setattr(runtime.subprocess,'run',lambda *a,**kw: calls.append(a) or type('R',(),{'returncode':1,'stdout':'','stderr':''})())
    archive=tmp_path/'Private Store/data/archive.sqlite3'
    result=runtime.install(archive,tmp_path/'Private Store/config.json',no_start=True)
    plist=Path(result['plist']); data=plistlib.loads(plist.read_bytes())
    assert data['StartInterval']==300 and not calls
    assert Path(data['ProgramArguments'][0]).is_absolute()
    assert data['ProgramArguments'][-1]=='capture'
    archive.parent.mkdir(parents=True,exist_ok=True); archive.write_text('preserve')
    unrelated=plist.parent/'local.knowledge-archive.plist'; unrelated.write_text('preserve')
    runtime.uninstall()
    assert not plist.exists() and unrelated.exists() and archive.read_text()=='preserve'


def test_capture_rediscovers_and_protects_source(tmp_path, monkeypatch, capsys):
    import sqlite3
    from context_archive.adapters import Source
    source=tmp_path/'History'; sqlite3.connect(source).close()
    before=source.read_bytes(); calls=[]
    monkeypatch.setattr(cli.adapters,'discover',lambda **kw: calls.append(kw) or {'sources':[Source(source,'chromium')],'issues':[]})
    for _ in range(2):
        assert cli.main(['capture','--archive',str(source)])==2
        capsys.readouterr()
    assert len(calls)==2 and source.read_bytes()==before


def test_doctor_is_read_only_and_reports_actual_interpreter(tmp_path, monkeypatch):
    import sqlite3
    import sys
    from context_archive.adapters import Source
    source=tmp_path/'db'; sqlite3.connect(source).close()
    monkeypatch.setattr(cli.adapters,'discover',lambda **kw: {'sources':[Source(source,'knowledge')],'issues':[]})
    monkeypatch.setattr(runtime,'background_status',lambda: {'loaded':False})
    before=source.read_bytes()
    result=cli.doctor({})
    assert result['permission_executable']==str(Path(sys.executable).resolve())
    assert result['sources'][0]['readable'] is True and source.read_bytes()==before


def test_installer_no_start_fake_tools(tmp_path):
    import os
    import subprocess
    fake=tmp_path/'bin'; fake.mkdir()
    (fake/'uname').write_text('#!/bin/sh\necho Darwin\n')
    (fake/'launchctl').write_text('#!/bin/sh\nexit 1\n')
    (fake/'uv').write_text('''#!/bin/sh
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
cat > "$UV_PROJECT_ENVIRONMENT/bin/context-archive" <<'EOF'
#!/bin/sh
printf '%s\\n' "$@" > "$CONTEXT_ARCHIVE_HOME/cli-args"
EOF
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/context-archive"
printf '%s\\n' "$@" > "$CONTEXT_ARCHIVE_HOME/uv-args"
''')
    for path in fake.iterdir(): path.chmod(0o755)
    root=tmp_path/'Private Store'
    env={**os.environ,'HOME':str(tmp_path),'CONTEXT_ARCHIVE_HOME':str(root),'PATH':str(fake)+':/usr/bin:/bin'}
    script=Path(__file__).resolve().parents[1]/'install.sh'
    subprocess.run(['sh',str(script),'--no-start'],env=env,check=True,capture_output=True)
    assert '--no-start' in (root/'cli-args').read_text().splitlines()
    assert '--locked' in (root/'uv-args').read_text().splitlines()
    assert '--no-editable' in (root/'uv-args').read_text().splitlines()
    env['CONTEXT_ARCHIVE_HOME']='relative'
    assert subprocess.run(['sh',str(script),'--no-start'],env=env,capture_output=True).returncode==2


def test_install_refuses_unrelated_plist_and_uninstall_keeps_loaded_agent(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('HOME',str(tmp_path)); monkeypatch.setenv('CONTEXT_ARCHIVE_HOME',str(tmp_path/'state'))
    path=runtime.agent_path(); path.parent.mkdir(parents=True)
    unrelated=plistlib.dumps({'Label':'other'}); path.write_bytes(unrelated)
    with pytest.raises(ValueError): runtime.install(tmp_path/'db',tmp_path/'config',no_start=True)
    assert path.read_bytes()==unrelated
    path.unlink()
    runtime.install(tmp_path/'db',tmp_path/'config',no_start=True)
    data=plistlib.loads(path.read_bytes())
    assert data['Disabled'] and not data['RunAtLoad']
    monkeypatch.setattr(runtime.subprocess,'run',lambda *a,**kw:type('R',(),{'returncode':1})())
    monkeypatch.setattr(runtime,'background_status',lambda:{'loaded':True})
    with pytest.raises(RuntimeError): runtime.uninstall()
    assert path.exists()


def test_no_start_refuses_loaded_service_before_writes(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path)); monkeypatch.setenv('CONTEXT_ARCHIVE_HOME',str(tmp_path/'state'))
    path=runtime.agent_path(); path.parent.mkdir(parents=True)
    before=plistlib.dumps({'Label':runtime.LABEL,'RunAtLoad':True}); path.write_bytes(before)
    monkeypatch.setattr(runtime,'background_status',lambda:{'loaded':True})
    with pytest.raises(RuntimeError,match='uninstall'):
        runtime.install(tmp_path/'db',tmp_path/'config',no_start=True)
    assert path.read_bytes()==before and not (tmp_path/'state').exists()


@pytest.mark.parametrize('flags',[[],['--no-start']])
def test_shell_refuses_loaded_service_before_runtime_mutations(tmp_path,flags):
    import os
    import subprocess
    fake=tmp_path/'bin'; fake.mkdir()
    (fake/'uname').write_text('#!/bin/sh\necho Darwin\n')
    (fake/'launchctl').write_text('#!/bin/sh\n[ "$1" = print ]\n')
    (fake/'uv').write_text('#!/bin/sh\ntouch "$HOME/uv-was-called"\n')
    for path in fake.iterdir(): path.chmod(0o755)
    plist=tmp_path/'Library/LaunchAgents/local.context-archive.plist'; plist.parent.mkdir(parents=True)
    before=plistlib.dumps({'Label':runtime.LABEL}); plist.write_bytes(before)
    env={**os.environ,'HOME':str(tmp_path),'CONTEXT_ARCHIVE_HOME':str(tmp_path/'state'),'PATH':str(fake)+':/usr/bin:/bin'}
    script=Path(__file__).resolve().parents[1]/'install.sh'
    result=subprocess.run(['sh',str(script),*flags],env=env,capture_output=True,text=True)
    assert result.returncode==1 and 'uninstall' in result.stderr
    assert plist.read_bytes()==before and not (tmp_path/'state').exists()
    assert not (tmp_path/'uv-was-called').exists()

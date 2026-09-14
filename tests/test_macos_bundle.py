"""Opt-in integration tests: CA_TEST_APP=/absolute/path/Context Archive.app.

Runs only against synthetic data. No registration, permissions, or real sources.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import pytest

APP=os.environ.get('CA_TEST_APP')
pytestmark=pytest.mark.skipif(sys.platform!='darwin' or not APP,reason='Set CA_TEST_APP to test an assembled app')


@pytest.fixture(scope='module')
def bundled(tmp_path_factory):
    root=tmp_path_factory.mktemp('relocated')
    app=root/'Moved App.app';shutil.copytree(APP,app,symlinks=True)
    home=root/'home';home.mkdir()
    state=root/'Private State'
    for relative,sql in [
        ('Knowledge/knowledgeC.db',"CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZUUID,ZSTREAMNAME,ZVALUESTRING,ZSTARTDATE,ZENDDATE); INSERT INTO ZOBJECT VALUES(1,'test','/app/usage','example.editor',0,30);"),
        ('Safari/History.db',"CREATE TABLE history_items(id INTEGER PRIMARY KEY,url);CREATE TABLE history_visits(id INTEGER PRIMARY KEY,history_item,visit_time,title);INSERT INTO history_items VALUES(1,'https://example.com/test');INSERT INTO history_visits VALUES(1,1,0,'Synthetic page');")]:
        path=home/'Library'/('Application Support' if relative.startswith('Knowledge') else '')/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path) as c:c.executescript(sql)
    env={'HOME':str(home),'CONTEXT_ARCHIVE_HOME':str(state),'PATH':'','PYTHONDONTWRITEBYTECODE':'1'}
    cli=app/'Contents/MacOS/context-archive'
    agent=app/'Contents/MacOS/ContextArchiveAgent'
    return root,app,state,env,cli,agent


def invoke(bundled,*args):
    root,_,_,env,cli,_=bundled
    result=subprocess.run([str(cli),*args],cwd=root,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr or result.stdout
    return json.loads(result.stdout)


def test_relocated_agent_pause_profiles_and_capture(bundled):
    root,app,state,env,cli,agent=bundled
    status=invoke(bundled,'control-status')
    assert not status['archive_exists'] and len(status['sources'])==2
    # launchd may supply a relative BundleProgram or service label as argv[0].
    result=subprocess.run(['io.github.urcades.ContextArchive.agent'],executable=str(agent),cwd=root,env=env,capture_output=True,timeout=30)
    assert result.returncode==0
    status=invoke(bundled,'control-status')
    assert status['latest_cycle']['status']=='completed'
    assert len(status['latest_cycle']['captures'])==2
    assert len(invoke(bundled,'timeline')['events'])==2
    invoke(bundled,'control-update','--params','{"paused":true}')
    subprocess.run([str(agent)],cwd=root,env=env,check=True,timeout=30)
    assert invoke(bundled,'control-status')['latest_cycle']['status']=='paused'
    invoke(bundled,'control-update','--params','{"paused":false}')
    source=status['sources'][0]['path']
    invoke(bundled,'control-update','--params',json.dumps({'source_path':source,'enabled':False}))
    assert next(s for s in invoke(bundled,'control-status')['sources'] if s['path']==source)['enabled'] is False
    # Running code must not write bytecode or any runtime state into the bundle.
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True,capture_output=True)


def test_bundled_mcp_stdio(bundled):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _,_,_,env,cli,_=bundled
    async def run():
        async with stdio_client(StdioServerParameters(command=str(cli),args=['mcp'],env=env)) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                listing=await session.list_tools()
                assert {'episodes','timeline','get_record'}<={t.name for t in listing.tools}
                result=await session.call_tool('timeline',{})
                assert not result.isError
                events=json.loads(result.content[0].text)['events']
                assert len(events)==2 and all(e['raw_refs'] for e in events)
    asyncio.run(run())

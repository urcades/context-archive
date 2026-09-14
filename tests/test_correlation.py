import json
import sqlite3
import pytest
from context_archive import core, query
from context_archive.adapters import Source
from context_archive import correlation

START='2001-01-01T00:00:00Z'
END='2001-01-02T00:00:00Z'


def fixture(tmp_path):
    archive=tmp_path/'archive'
    for profile in ('one','two'):
        p=tmp_path/profile
        with sqlite3.connect(p) as c:
            c.executescript("CREATE TABLE history_items(id INTEGER PRIMARY KEY,url); CREATE TABLE history_visits(id INTEGER PRIMARY KEY,history_item,visit_time,title); INSERT INTO history_items VALUES(1,'https://example.com/repo');")
            c.executemany('INSERT INTO history_visits VALUES(?,1,?,?)',[(1,0,'Build'),(2,30,'Build'),(3,3600,'Later')])
        core.capture(Source(p,'safari','Safari',profile),archive)
    return archive


def test_episodes_keep_sources_and_evidence_distinct(tmp_path):
    a=fixture(tmp_path)
    result=query.episodes(a,start=START,end=END)
    assert len(result['episodes'])==2
    first=result['episodes'][0]
    assert first['event_count']==4
    assert len(first['lanes'])==2
    assert first['span_seconds']==30
    assert first['relationship']=='temporal_proximity'
    assert all(e['raw_refs'] for e in first['events'])
    assert 'attention' in result['limitations']


def test_registry_matches_are_explained_and_not_exclusive(tmp_path):
    a=fixture(tmp_path)
    for name in ('alpha','beta'):
        correlation.put_project(a,{'id':name,'name':name,'rules':[{'field':'url','operator':'prefix','value':'https://example.com/repo'}]})
    result=query.project_timeline(a,'alpha',start=START,end=END)
    assert len(result['events'])==6
    assert len(result['events'][0]['project_matches'])==2
    assert result['events'][0]['project_matches'][0]['rule_indexes']==[0]
    assert query.projects(a)['projects'][0]['id']=='alpha'
    core.reindex(a)
    assert len(query.projects(a)['projects'])==2
    from context_archive.transfer import export_archive
    export_archive(a,tmp_path/'export')
    rows=[json.loads(x) for x in (tmp_path/'export').read_text().splitlines()]
    assert len([r for r in rows if r['type']=='projects'])==2


def test_pagination_fixed_capture_and_rule_revision(tmp_path):
    a=fixture(tmp_path)
    p={'id':'a','name':'A','rules':[{'field':'domain','operator':'exact','value':'example.com'}]}
    correlation.put_project(a,p)
    first=query.episodes(a,start=START,end=END,limit=1)
    assert first['next_cursor']
    core.capture(Source(tmp_path/'one','safari','Safari','one'),a)
    second=query.episodes(a,start=START,end=END,limit=1,cursor=first['next_cursor'])
    assert second['coverage']['through_capture']==first['coverage']['through_capture']
    assert second['episodes'][0]['event_count']==2
    p['name']='Changed';correlation.put_project(a,p)
    with pytest.raises(ValueError):query.episodes(a,start=START,end=END,cursor=first['next_cursor'])


def test_validation_and_old_archive_readonly(tmp_path):
    a=fixture(tmp_path)
    assert query.projects(a)['projects']==[]
    with pytest.raises(ValueError):query.episodes(a,start='2001-01-01',end=END)
    with pytest.raises(ValueError):query.episodes(a,start=START,end=END,gap_seconds=-1)
    with pytest.raises(ValueError):correlation.put_project(a,{'id':'a','name':'A','rules':[{'field':'title','operator':'regex','value':'.*'}]})
    with pytest.raises(ValueError):query.project_timeline(a,'missing',start=START,end=END)


def test_long_intervals_do_not_bridge_visits_and_revisions_replace_matches(tmp_path):
    a=fixture(tmp_path)
    p=tmp_path/'knowledge'
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZUUID,ZSTREAMNAME,ZVALUESTRING,ZSTARTDATE,ZENDDATE); INSERT INTO ZOBJECT VALUES(1,'one','/app/usage','editor',-100,8000);")
    core.capture(Source(p,'knowledge'),a)
    result=query.episodes(a,start=START,end=END)
    assert len(result['episodes'])==2
    assert result['episodes'][0]['event_count']==5
    assert result['episodes'][0]['span_seconds']==30
    definition={'id':'build','name':'Build','rules':[{'field':'title','operator':'contains','value':'Build'}]}
    correlation.put_project(a,definition)
    first=query.project_timeline(a,'build',start=START,end=END,limit=1)
    with sqlite3.connect(tmp_path/'one') as c:
        c.execute("UPDATE history_visits SET title='Revised' WHERE id=2")
        c.execute("INSERT INTO history_visits VALUES(4,1,10,'Build new')")
    core.capture(Source(tmp_path/'one','safari','Safari','one'),a)
    old=query.project_timeline(a,'build',start=START,end=END,limit=100,cursor=first['next_cursor'])
    assert len(old['events'])==3
    assert all(e['title']=='Build' for e in old['events'])
    current=query.project_timeline(a,'build',start=START,end=END)
    assert len(current['events'])==4
    assert any(e['title']=='Build new' for e in current['events'])


def test_literal_prefix_disabled_registry_and_cli(tmp_path,capsys):
    from context_archive import cli
    a=fixture(tmp_path)
    p={'id':'repo','name':'Repo','rules':[{'field':'url','operator':'prefix','value':'https://example.com/re'}]}
    path=tmp_path/'project.json';path.write_text(json.dumps(p))
    assert cli.main(['--archive',str(a),'put-project',str(path)])==0
    capsys.readouterr()
    assert query.project_timeline(a,'repo',start=START,end=END)['events']==[]
    p['rules'][0]['value']='https://example.com/repo';correlation.put_project(a,p)
    assert len(query.project_timeline(a,'repo',start=START,end=END)['events'])==6
    p['enabled']=False;correlation.put_project(a,p)
    assert query.episodes(a,start=START,end=END)['episodes'][0]['events'][0]['project_matches']==[]
    with pytest.raises(ValueError):query.project_timeline(a,'repo',start=START,end=END)


def test_episode_max_span_reindex_cursor_and_size_budget(tmp_path):
    a=fixture(tmp_path)
    result=query.episodes(a,start=START,end=END,max_span_seconds=10,limit=1)
    assert result['total']==3
    core.reindex(a)
    with pytest.raises(ValueError):query.episodes(a,start=START,end=END,max_span_seconds=10,cursor=result['next_cursor'])
    with pytest.raises(ValueError):query.episodes(a,start=START,end='2002-01-01T00:00:00Z')


def test_large_episode_fails_explicitly_instead_of_dropping_evidence(tmp_path):
    a=fixture(tmp_path)
    with sqlite3.connect(tmp_path/'one') as c:
        c.executemany('INSERT INTO history_visits VALUES(?,1,0,?)',[(i,'x'*2048) for i in range(10,100)])
    core.capture(Source(tmp_path/'one','safari','Safari','one'),a)
    with pytest.raises(ValueError,match='response budget'):
        query.episodes(a,start=START,end=END)
    # Individual records remain accessible using the existing bounded timeline.
    assert query.timeline(a,start=START,end=END,limit=10)['next_cursor']


def test_registry_backup_and_read_only_access(tmp_path):
    from context_archive.transfer import backup
    a=fixture(tmp_path)
    before=a.read_bytes()
    query.projects(a);query.episodes(a,start=START,end=END)
    assert a.read_bytes()==before
    correlation.put_project(a,{'id':'a','name':'A','rules':[{'field':'domain','operator':'exact','value':'example.com'}]})
    backup(a,tmp_path/'copy')
    assert query.projects(tmp_path/'copy')==query.projects(a)

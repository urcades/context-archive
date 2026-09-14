import json
import sqlite3
import pytest
from context_archive import core, query
from context_archive.adapters import Source


def safari(tmp_path):
    p=tmp_path/'safari'; a=tmp_path/'archive'
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE history_items(id INTEGER PRIMARY KEY,url TEXT); CREATE TABLE history_visits(id INTEGER PRIMARY KEY,history_item,visit_time,title,origin); INSERT INTO history_items VALUES(1,'https://example.com/research'); INSERT INTO history_visits VALUES(1,1,0,'Research one',1); INSERT INTO history_visits VALUES(2,1,1,'Research two',0);")
    s=Source(p,'safari','Safari','profile-full')
    core.capture(s,a)
    return p,a,s


def test_stable_pagination_while_new_capture_revises_event(tmp_path):
    p,a,s=safari(tmp_path)
    first=query.timeline(a,limit=1)
    assert first['events'][0]['title']=='Research one'
    assert first['next_cursor']
    with sqlite3.connect(p) as c:
        c.execute("UPDATE history_visits SET title='Changed' WHERE id=2")
        c.execute("INSERT INTO history_visits VALUES(3,1,-10,'Late old synced visit',1)")
    core.capture(s,a)
    second=query.timeline(a,limit=1,cursor=first['next_cursor'])
    assert second['events'][0]['title']=='Research two'
    assert query.timeline(a)['events'][0]['title']=='Late old synced visit'
    assert len(query.search(a,'research')['events'])==3
    assert query.search(a,'Changed')['events'][0]['title']=='Changed'
    assert not query.search(a,'two')['events']
    with pytest.raises(ValueError): query.timeline(a,domain='elsewhere',cursor=first['next_cursor'])


def test_boundaries_profile_filters_and_raw_retrieval(tmp_path):
    p,a,s=safari(tmp_path)
    result=query.timeline(a,start='2000-12-31T19:00:00-05:00',end='2001-01-01T00:00:01Z',profile='profile-full')
    assert len(result['events'])==1
    assert result['events'][0]['fidelity']=='raw_database'
    ref=result['events'][0]['raw_refs'][0]
    assert query.get_record(a,ref)['payload']['origin']==1
    assert query.domain_summary(a)['domains'][0]['visits']==2
    with pytest.raises(ValueError): query.timeline(a,start='2001-01-01')
    with pytest.raises(ValueError): query.timeline(a,limit=0)
    assert query.timeline(a,profile='unknown')['events']==[]


def test_app_union_and_pruning_reindex(tmp_path):
    p=tmp_path/'knowledge'; a=tmp_path/'archive'
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZUUID,ZSTREAMNAME,ZVALUESTRING,ZSTARTDATE,ZENDDATE); INSERT INTO ZOBJECT VALUES(1,'one','/app/usage','editor',0,10); INSERT INTO ZOBJECT VALUES(2,'two','/app/usage','editor',5,15);")
    s=Source(p,'knowledge'); core.capture(s,a)
    with sqlite3.connect(p) as c:c.execute('DELETE FROM ZOBJECT WHERE Z_PK=1')
    core.capture(s,a)
    summary=query.app_usage_summary(a,start='2001-01-01T00:00:02Z',end='2001-01-01T00:00:12Z')
    assert summary['apps'][0]['recorded_seconds']==15
    assert summary['apps'][0]['union_seconds']==10
    before=query.timeline(a)['events']; core.reindex(a)
    assert query.timeline(a)['events']==before


def test_query_denies_side_effects_and_bounds_output(tmp_path):
    _,a,_=safari(tmp_path)
    assert query.query(a,'SELECT count(*) FROM records')['rows'][0][0]==3
    for sql in ('DELETE FROM records','PRAGMA journal_mode=DELETE',"SELECT load_extension('/tmp/no')",'ATTACH DATABASE ":memory:" AS x'):
        with pytest.raises((ValueError,sqlite3.Error)):query.query(a,sql)
    with pytest.raises((ValueError,sqlite3.Error)):query.query(a,'SELECT randomblob(10000000)')
    assert query.query(a,'SELECT 1 UNION ALL SELECT 2',limit=1)['next_offset']==1


def test_blob_chunking_and_safe_decoder(tmp_path):
    p=tmp_path/'raw';a=tmp_path/'archive'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE raw(v)');c.execute('INSERT INTO raw VALUES(?)',(b'{"hello":"world"}',))
    core.capture(Source(p,'knowledge'),a)
    with core.readonly(a) as c:h=c.execute('SELECT hash FROM blobs').fetchone()[0]
    assert query.get_blob(a,h,limit=2)['next_offset']==2
    assert 'world' in query.decode_blob(a,h)['decoded_chunk']


def test_reindex_invalidates_old_cursor(tmp_path):
    _,a,_=safari(tmp_path)
    cursor=query.timeline(a,limit=1)['next_cursor']
    core.reindex(a)
    with pytest.raises(ValueError,match='obsolete'):query.timeline(a,cursor=cursor)


def test_mcp_sized_summary_and_long_titles_are_bounded(tmp_path):
    p=tmp_path/'large';a=tmp_path/'archive'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZUUID,ZSTREAMNAME,ZVALUESTRING,ZSTARTDATE,ZENDDATE)')
        c.executemany('INSERT INTO ZOBJECT VALUES(?,?,?,?,?,?)',[(i,str(i),'/app/usage',str(i)+'x'*100000,0,1) for i in range(5)])
    core.capture(Source(p,'knowledge'),a)
    assert len(json.dumps(query.app_usage_summary(a)))<200000
    assert len(json.dumps(query.timeline(a)))<200000


def test_sql_blob_result_is_safe_preview(tmp_path):
    _,a,_=safari(tmp_path)
    assert query.query(a,"SELECT x'ff'")['rows'][0][0]['size']==1


def test_pruned_browser_join_retains_archived_metadata_and_reindexes(tmp_path):
    p,a,s=safari(tmp_path)
    with sqlite3.connect(p) as c:c.execute('DELETE FROM history_items')
    core.capture(s,a)
    assert query.timeline(a)['events'][0]['url']=='https://example.com/research'
    assert len(query.search(a,'example')['events'])==2
    core.reindex(a)
    assert query.domain_summary(a)['domains'][0]['domain']=='example.com'


def test_collection_identity_is_separate_from_origin(tmp_path):
    _,a,_=safari(tmp_path)
    result=query.timeline(a)['events'][0]
    assert result['collected_by']
    assert result['device'] is None
    assert query.get_record(a,result['raw_refs'][0])['payload']['origin']==1

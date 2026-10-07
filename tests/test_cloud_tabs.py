import sqlite3
from context_archive import core, query
from context_archive.adapters import Source, discover


def test_cloud_tabs_observations_preserve_identity_without_visit_counts(tmp_path):
    path=tmp_path/'CloudTabs.db'; archive=tmp_path/'archive.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TABLE cloud_tab_devices(device_uuid TEXT PRIMARY KEY, device_name TEXT, device_type_identifier TEXT); CREATE TABLE cloud_tabs(tab_uuid TEXT PRIMARY KEY, device_uuid TEXT, url TEXT, title TEXT, last_viewed_time REAL, system_fields BLOB);")
        c.execute('INSERT INTO cloud_tab_devices VALUES(?,?,?)',('phone','Phone','phone-model'))
        c.executemany('INSERT INTO cloud_tabs VALUES(?,?,?,?,?,?)',[(t,'phone','https://example.org','Title',123,b'secret binary') for t in ['a','b']])
    source=Source(path,'safari_tabs','Safari','iCloud Tabs')
    core.capture(source,archive);core.capture(source,archive)
    with sqlite3.connect(archive) as c:
        assert c.execute('select count(*) from events').fetchone()[0]==4
        assert c.execute('select count(*) from blobs').fetchone()[0]==0
        assert c.execute("select count(*) from events where kind='browser_visit'").fetchone()[0]==0
        assert c.execute("select count(*) from events where started_at is null").fetchone()[0]==0
        before=c.execute('select event_key,payload_json from events order by id').fetchall()
    core.reindex(archive)
    with sqlite3.connect(archive) as c:
        assert before==c.execute('select event_key,payload_json from events order by id').fetchall()
    with sqlite3.connect(path) as c:c.execute("delete from cloud_tabs where tab_uuid='a'")
    core.capture(source,archive)
    with sqlite3.connect(archive) as c:assert c.execute('select count(*) from events').fetchone()[0]==5
    events=query.timeline(archive,kind='browser_tab_observation')['events']
    assert len(events)==5 and all(e['raw_refs'] for e in events)


def test_cloud_tabs_discovery(tmp_path):
    path=tmp_path/'Library/Containers/com.apple.Safari/Data/Library/Safari/CloudTabs.db'
    path.parent.mkdir(parents=True);path.touch()
    found=discover(tmp_path)['sources']
    assert len(found)==1 and found[0].adapter=='safari_tabs'

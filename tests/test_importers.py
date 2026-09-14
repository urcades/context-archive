import json
import sqlite3
import sys
import hashlib
from contextlib import closing
import pytest
from context_archive import core
from context_archive.importers import import_browser, import_knowledge


def test_browser_append_and_malformed(tmp_path):
    source=tmp_path/'visits.jsonl'; archive=tmp_path/'archive.db'
    line=json.dumps({'timestamp':'2026-01-01T00:00:00Z','url':'https://example.org','browser':'Dia','extra':2**60}).encode()+b'\n'
    source.write_bytes(line+line+b'bad\n')
    assert import_browser(source,archive)['records']==2
    assert import_browser(source,archive)['records']==0
    source.write_bytes(source.read_bytes()+line)
    assert import_browser(source,archive)['records']==1
    with core.readonly(archive) as c:
        assert c.execute('select count(*) from events').fetchone()[0]==3
        assert c.execute('select count(*) from import_lines').fetchone()[0]==7
        assert c.execute('select count(*) from import_lines where error is not null').fetchone()[0]==2


def test_knowledge_incremental_and_corruption(tmp_path):
    source=tmp_path/'source.db'; legacy=tmp_path/'legacy.db'; archive=tmp_path/'archive.db'
    with sqlite3.connect(source) as c:
        c.executescript("CREATE TABLE Z_METADATA(Z_UUID TEXT); INSERT INTO Z_METADATA VALUES('store'); CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZSTREAMNAME TEXT,ZVALUESTRING TEXT); INSERT INTO ZOBJECT VALUES(1,'/app/usage','app');")
    legacy_capture(source,legacy)
    assert import_knowledge(legacy,archive)['captures']==1
    assert import_knowledge(legacy,archive)['captures']==0
    with sqlite3.connect(source) as c: c.execute('DELETE FROM ZOBJECT')
    legacy_capture(source,legacy)
    assert import_knowledge(legacy,archive)['captures']==1
    with core.readonly(archive) as c:
        assert c.execute('SELECT count(*) FROM changes WHERE version_id IS NULL').fetchone()[0]==1
    with sqlite3.connect(legacy) as c: c.execute("UPDATE records SET content_hash='broken'")
    with pytest.raises(ValueError,match='hash'): import_knowledge(legacy,archive)


def test_import_then_native_does_not_duplicate_events(tmp_path):
    from context_archive.adapters import Source
    source=tmp_path/'source.db'; legacy=tmp_path/'legacy.db'; archive=tmp_path/'archive.db'
    with sqlite3.connect(source) as c:
        c.executescript("CREATE TABLE Z_METADATA(Z_UUID TEXT); INSERT INTO Z_METADATA VALUES('store'); CREATE TABLE ZOBJECT(Z_PK INTEGER PRIMARY KEY,ZSTREAMNAME TEXT,ZVALUESTRING TEXT); INSERT INTO ZOBJECT VALUES(1,'/app/usage','app');")
    legacy_capture(source,legacy); import_knowledge(legacy,archive)
    assert core.capture(Source(source,'knowledge'),archive)['changed']==0
    with core.readonly(archive) as c:
        assert c.execute('SELECT count(*) FROM events').fetchone()[0]==1
    assert import_knowledge(legacy,archive)['captures']==0
    legacy_capture(source,legacy)
    with pytest.raises(ValueError,match='after native collection'): import_knowledge(legacy,archive)
    with core.readonly(archive) as c:
        assert c.execute('SELECT count(*) FROM captures').fetchone()[0]==2


def test_browser_retains_bytes_and_unknown_fields(tmp_path):
    source=tmp_path/'log'; archive=tmp_path/'archive'
    original=b'{"url":"https://example.org", "extra":1152921504606846976}\r\n'
    source.write_bytes(original+b'\xff\n')
    result=import_browser(source,archive)
    assert result['malformed']==1
    assert source.read_bytes()==original+b'\xff\n'
    with core.readonly(archive) as c:
        assert c.execute('SELECT raw FROM import_lines WHERE line_number=1').fetchone()[0]==original
        assert json.loads(c.execute('SELECT payload_json FROM records').fetchone()[0])['extra']==2**60
        assert tuple(c.execute('SELECT browser,profile,device,fidelity FROM sources').fetchone())==(None,None,None,'legacy_visit')


def test_browser_numeric_overflow_and_depth_do_not_abort_valid_lines(tmp_path,monkeypatch):
    source=tmp_path/'log'; archive=tmp_path/'archive'
    good=b'{"url":"https://example.org", "large":1152921504606846976}\n'
    overflow=b'{"value":1e999}\n'
    depth=sys.getrecursionlimit()+100
    deep=b'{"value":'+b'['*depth+b'0'+b']'*depth+b'}\n'
    original_loads=json.loads
    def limited_loads(value, **kwargs):
        if value==deep: raise RecursionError('Synthetic decoder nesting limit')
        return original_loads(value, **kwargs)
    monkeypatch.setattr(json,'loads',limited_loads)
    source.write_bytes(good+overflow+deep+good)
    result=import_browser(source,archive)
    assert result['records']==2
    assert result['malformed']==2
    with core.readonly(archive) as c:
        rows=c.execute('SELECT raw,error FROM import_lines ORDER BY line_number').fetchall()
        assert rows[1]['raw']==overflow and rows[1]['error']
        assert rows[2]['raw']==deep and rows[2]['error']
        assert all(json.loads(r[0])['large']==2**60 for r in c.execute('SELECT payload_json FROM records'))


def test_invalid_knowledge_uuid_does_not_merge_unrelated_stores(tmp_path):
    legacy=tmp_path/'legacy.db'; archive=tmp_path/'archive.db'
    for name in ('first','second'):
        source=tmp_path/name
        with sqlite3.connect(source) as c:
            c.executescript('CREATE TABLE Z_METADATA(Z_UUID TEXT); INSERT INTO Z_METADATA VALUES(NULL);')
        legacy_capture(source,legacy)
    assert import_knowledge(legacy,archive)['captures']==2
    with core.readonly(archive) as c:
        assert c.execute('SELECT count(*) FROM sources').fetchone()[0]==2


# Independent version-1 fixture contract: no old installation or current archive
# implementation is used to manufacture the expected legacy evidence.
LEGACY_SCHEMA = '''
CREATE TABLE archive_info(version INTEGER NOT NULL, archive_id TEXT NOT NULL);
INSERT INTO archive_info VALUES(1,'synthetic-legacy-archive');
CREATE TABLE sources(id TEXT PRIMARY KEY,path TEXT NOT NULL,identity TEXT NOT NULL);
CREATE TABLE captures(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),
 observed_at TEXT NOT NULL,schema_json TEXT NOT NULL,row_count INTEGER NOT NULL,
 changed_count INTEGER NOT NULL,deleted_count INTEGER NOT NULL);
CREATE TABLE records(version_id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),
 table_name TEXT NOT NULL,row_key TEXT NOT NULL,content_hash TEXT NOT NULL,
 first_capture INTEGER NOT NULL REFERENCES captures(id),payload_json TEXT NOT NULL,
 UNIQUE(source_id,table_name,row_key,content_hash));
CREATE TABLE changes(capture_id INTEGER NOT NULL REFERENCES captures(id),table_name TEXT NOT NULL,
 row_key TEXT NOT NULL,version_id INTEGER REFERENCES records(version_id),
 PRIMARY KEY(capture_id,table_name,row_key));
CREATE TABLE current_rows(source_id TEXT NOT NULL REFERENCES sources(id),table_name TEXT NOT NULL,
 row_key TEXT NOT NULL,version_id INTEGER NOT NULL REFERENCES records(version_id),
 PRIMARY KEY(source_id,table_name,row_key));
CREATE TABLE blobs(hash TEXT PRIMARY KEY,data BLOB NOT NULL,size INTEGER NOT NULL);
'''


def legacy_capture(source, archive):
    """Capture only our simple synthetic tables using the old wire format."""
    def wire(value):
        return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
    def sha(value):
        return hashlib.sha256(value.encode() if isinstance(value,str) else value).hexdigest()
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(archive)) as dst:
        src.row_factory=sqlite3.Row
        if not dst.execute("SELECT 1 FROM sqlite_master WHERE name='archive_info'").fetchone():
            dst.executescript(LEGACY_SCHEMA)
        identity=str(src.execute('SELECT Z_UUID FROM Z_METADATA').fetchone()[0])
        sid=sha(str(source)+'\0'+identity)
        dst.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?)',(sid,str(source),identity))
        schema=[dict(r) for r in src.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        cid=dst.execute('INSERT INTO captures(source_id,observed_at,schema_json,row_count,changed_count,deleted_count) VALUES(?,?,?,0,0,0)',
                        (sid,'2026-01-01T00:00:00+00:00',wire(schema))).lastrowid
        previous={(r[0],r[1]):r[2] for r in dst.execute('SELECT cr.table_name,cr.row_key,r.content_hash FROM current_rows cr JOIN records r USING(version_id) WHERE cr.source_id=?',(sid,))}
        seen=set(); changed=0
        for table in schema:
            if table['type']!='table': continue
            name=table['name']; quoted='"'+name.replace('"','""')+'"'
            columns=src.execute('PRAGMA table_info('+quoted+')').fetchall()
            pk=[r['name'] for r in columns if r['pk']]
            for row in src.execute('SELECT rowid AS synthetic_rowid,* FROM '+quoted):
                payload={}
                for col in columns:
                    value=row[col['name']]
                    if isinstance(value,bytes):
                        h=sha(value); dst.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)',(h,value,len(value)))
                        value={'$blob':h,'size':len(value)}
                    payload[col['name']]=value
                key=wire([payload[k] for k in pk] if pk else ['$rowid',row['synthetic_rowid']])
                pair=(name,key); seen.add(pair); serialized=wire(payload); h=sha(serialized)
                if previous.get(pair)==h: continue
                dst.execute('INSERT OR IGNORE INTO records(source_id,table_name,row_key,content_hash,first_capture,payload_json) VALUES(?,?,?,?,?,?)',(sid,name,key,h,cid,serialized))
                vid=dst.execute('SELECT version_id FROM records WHERE source_id=? AND table_name=? AND row_key=? AND content_hash=?',(sid,name,key,h)).fetchone()[0]
                dst.execute('INSERT INTO changes VALUES(?,?,?,?)',(cid,name,key,vid))
                dst.execute('INSERT OR REPLACE INTO current_rows VALUES(?,?,?,?)',(sid,name,key,vid)); changed+=1
        gone=previous.keys()-seen
        for name,key in gone:
            dst.execute('INSERT INTO changes VALUES(?,?,?,NULL)',(cid,name,key))
            dst.execute('DELETE FROM current_rows WHERE source_id=? AND table_name=? AND row_key=?',(sid,name,key))
        dst.execute('UPDATE captures SET row_count=?,changed_count=?,deleted_count=? WHERE id=?',(len(seen),changed,len(gone),cid))
        dst.commit()


def test_knowledge_blob_bytes_and_hash_survive_import(tmp_path):
    source=tmp_path/'source'; legacy=tmp_path/'legacy'; archive=tmp_path/'archive'
    original=b'\x00\xffsynthetic\x00blob'
    with sqlite3.connect(source) as c:
        c.executescript("CREATE TABLE Z_METADATA(Z_UUID TEXT); INSERT INTO Z_METADATA VALUES('blob-store'); CREATE TABLE ZDATA(id INTEGER PRIMARY KEY,data BLOB);")
        c.execute('INSERT INTO ZDATA VALUES(1,?)',(original,))
    legacy_capture(source,legacy)
    import_knowledge(legacy,archive)
    with core.readonly(archive) as c:
        blob=c.execute('SELECT * FROM blobs').fetchone()
        assert blob['data']==original
        assert blob['hash']==hashlib.sha256(original).hexdigest()
        assert blob['size']==len(original)
        payload=json.loads(c.execute("SELECT payload_json FROM records WHERE table_name='ZDATA'").fetchone()[0])
        assert payload['data']=={'$blob':blob['hash'],'size':len(original)}

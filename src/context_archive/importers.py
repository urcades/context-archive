"""Atomic, lossless imports. Inputs are snapshotted and never modified."""
from pathlib import Path
import json
import sqlite3
from . import core
from .adapters import Source


def import_knowledge(legacy: Path, archive: Path) -> dict:
    core.protect(archive,[legacy])
    count=0; warnings=[]
    with core.writer(archive) as c, core.snapshot(legacy) as old:
        required={'archive_info','sources','captures','records','changes','current_rows','blobs'}
        tables={r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables!=required: raise ValueError('Incompatible Knowledge Archive schema')
        try:
            info=old.execute('SELECT version,archive_id FROM archive_info').fetchall()
            if len(info)!=1 or info[0]['version']!=1 or not info[0]['archive_id']: raise ValueError('Unsupported legacy archive version or identity')
            aid=info[0]['archive_id']
            if old.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('Legacy foreign key integrity failure')
            for b in old.execute('SELECT * FROM blobs'):
                if core.digest(b['data'])!=b['hash'] or len(b['data'])!=b['size']: raise ValueError('Legacy blob hash corruption')
                c.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)',tuple(b))
            for r in old.execute('SELECT * FROM records'):
                if core.digest(r['payload_json'])!=r['content_hash']: raise ValueError('Legacy record hash corruption')
                payload=json.loads(r['payload_json'])
                if not isinstance(payload,dict): raise ValueError('Invalid legacy record payload')
                for value in payload.values():
                    if isinstance(value,dict) and '$blob' in value:
                        b=old.execute('SELECT size FROM blobs WHERE hash=?',(value['$blob'],)).fetchone()
                        if b is None or b[0]!=value.get('size'): raise ValueError('Missing or corrupt legacy blob reference')
            import_id='knowledge:'+aid
            prior=c.execute('SELECT details_json FROM imports WHERE id=?',(import_id,)).fetchone()
            details=json.loads(prior[0]) if prior else {'path':str(Path(legacy).resolve()),'sources':{},'captures':{},'versions':{}}
            sources={}
            for s in old.execute('SELECT * FROM sources'):
                sid=core.identity_key('knowledge',s['identity'],Path(s['path']))
                source=Source(Path(s['path']),'knowledge')
                sources[s['id']]=(sid,source)
                core.register_source(c,source,sid,s['identity'])
                details['sources'][s['id']]=sid
            for cap in old.execute('SELECT * FROM captures ORDER BY id'):
                ref=f"{aid}:{cap['id']}"
                if c.execute('SELECT 1 FROM captures WHERE legacy_ref=?',(ref,)).fetchone(): continue
                sid,source=sources[cap['source_id']]
                # Appending historical observations after native snapshots would rewind
                # current_rows and corrupt subsequent diff semantics. Fail atomically.
                if c.execute("SELECT 1 FROM captures WHERE source_id=? AND provenance!='legacy_knowledge' LIMIT 1",(sid,)).fetchone():
                    raise ValueError('Cannot import past Knowledge captures after native collection for this source; import into a fresh archive before collecting')
                if c.execute("SELECT 1 FROM captures WHERE source_id=? AND provenance='legacy_knowledge' AND legacy_ref NOT LIKE ? LIMIT 1",(sid,aid+':%')).fetchone():
                    raise ValueError('Cannot merge overlapping Knowledge archive histories for the same source; use a fresh archive')
                schema=json.loads(cap['schema_json'])
                if not isinstance(schema,list): raise ValueError('Invalid legacy source schema')
                cid=c.execute('INSERT INTO captures(source_id,observed_at,schema_json,row_count,changed_count,deleted_count,provenance,legacy_ref) VALUES(?,?,?,?,?,?,?,?)',
                    (sid,cap['observed_at'],cap['schema_json'],cap['row_count'],cap['changed_count'],cap['deleted_count'],'legacy_knowledge',ref)).lastrowid
                details['captures'][str(cap['id'])]=cid
                for ch in old.execute('SELECT * FROM changes WHERE capture_id=?',(cap['id'],)):
                    if ch['version_id'] is None:
                        c.execute('INSERT INTO changes VALUES(?,?,?,NULL)',(cid,ch['table_name'],ch['row_key']))
                        c.execute('DELETE FROM current_rows WHERE source_id=? AND table_name=? AND row_key=?',(sid,ch['table_name'],ch['row_key']))
                    else:
                        r=old.execute('SELECT * FROM records WHERE version_id=?',(ch['version_id'],)).fetchone()
                        if r is None or (r['source_id'],r['table_name'],r['row_key'])!=(cap['source_id'],ch['table_name'],ch['row_key']): raise ValueError('Legacy change identity mismatch')
                        vid=core.observe(c,sid,cid,r['table_name'],r['row_key'],r['payload_json'])
                        details['versions'][str(r['version_id'])]=vid
                actual=c.execute('SELECT count(*) FROM current_rows WHERE source_id=?',(sid,)).fetchone()[0]
                if actual!=cap['row_count']: raise ValueError('Legacy capture membership count mismatch')
                warnings.extend(core.project(c,source,sid,cid)); count+=1
            c.execute('INSERT OR REPLACE INTO imports VALUES(?,?,?,?)',(import_id,'legacy_knowledge',core.now(),core.canonical(details)))
        except (sqlite3.Error,KeyError,TypeError,json.JSONDecodeError) as e:
            raise ValueError('Incompatible or corrupt Knowledge Archive: '+str(e)) from e
    return {'captures':count,'warnings':sorted(set(warnings))}


def import_browser(legacy: Path, archive: Path) -> dict:
    core.protect(archive,[legacy])
    path=Path(legacy).expanduser().resolve(strict=True)
    # The immutable bytes are the artifact, including malformed lines and endings.
    raw=path.read_bytes(); artifact=core.digest(raw); lines=raw.splitlines(keepends=True)
    warnings=[]; records=0
    with core.writer(archive) as c:
        if c.execute('SELECT 1 FROM imports WHERE id=?',(artifact,)).fetchone():
            return {'records':0,'captures':0,'malformed':0,'warnings':[],'import_id':artifact}
        lineage=artifact; skip=0
        # Reuse a verified whole-line prefix from this same path. A rewritten or
        # truncated file starts a separate lineage; identical lines remain distinct.
        for row in c.execute("SELECT id,details_json FROM imports WHERE kind='legacy_browser'"):
            detail=json.loads(row['details_json'])
            size=detail['size']
            if detail['path']==str(path) and size<=len(raw) and size>skip and core.digest(raw[:size])==row['id']:
                if size==0 or raw[size-1:size] in (b'\n',b'\r'):
                    skip=size; lineage=detail['lineage']
        details={'path':str(path),'size':len(raw),'lineage':lineage,'prefix_bytes':skip}
        c.execute('INSERT INTO imports VALUES(?,?,?,?)',(artifact,'legacy_browser',core.now(),core.canonical(details)))
        captures={}; offset=0; malformed=0
        for number,line in enumerate(lines,1):
            error=None; payload=None
            try:
                payload=json.loads(line,parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite JSON constant')))
                if not isinstance(payload,dict): raise ValueError('Expected JSON object')
                serialized=core.canonical(payload)
            except (ValueError,UnicodeDecodeError,RecursionError) as e:
                error=str(e); malformed+=1; warnings.append(f'Line {number}: {error}')
            c.execute('INSERT INTO import_lines VALUES(?,?,?,?)',(artifact,number,line,error))
            old_line=offset<skip; offset+=len(line)
            if error or old_line: continue
            browser=payload.get('browser'); device=payload.get('device')
            browser=browser if isinstance(browser,str) else None
            device=device if isinstance(device,str) else None
            sid=core.digest(core.canonical(['legacy_browser',lineage,browser,device]))
            source=Source(path,'legacy_browser',browser,None,device)
            if sid not in captures:
                core.register_source(c,source,sid,lineage,'legacy_visit')
                cid=c.execute("INSERT INTO captures(source_id,observed_at,schema_json,provenance,legacy_ref) VALUES(?,?,?,?,?)",(sid,core.now(),'[]','legacy_browser',artifact+':'+sid)).lastrowid
                captures[sid]=(cid,source,0)
            cid,source,n=captures[sid]
            core.observe(c,sid,cid,'visits',core.canonical([number]),serialized)
            captures[sid]=(cid,source,n+1); records+=1
        for sid,(cid,source,n) in captures.items():
            total=c.execute('SELECT count(*) FROM current_rows WHERE source_id=?',(sid,)).fetchone()[0]
            c.execute('UPDATE captures SET row_count=?,changed_count=? WHERE id=?',(total,n,cid))
            warnings.extend(core.project(c,source,sid,cid))
    return {'records':records,'captures':len(captures),'malformed':malformed,'warnings':warnings,'import_id':artifact}

"""Read-only evidence retrieval with fixed-observation pagination."""
from __future__ import annotations
import base64
import hashlib
import datetime as dt
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import sqlite3
import time
from .core import readonly, canonical, digest
MAX_CELL = 60000


def connect(path):
    c=readonly(path)
    # Bounds apply to every public query, including FTS, not just the SQL escape hatch.
    deadline=time.monotonic()+5
    c.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
    return c


def bound(value, maximum=500):
    if not 1 <= value <= maximum:
        raise ValueError(f'limit must be between 1 and {maximum}')
    return value


def scalar(v):
    if isinstance(v, bytes):
        return {'$blob': hashlib.sha256(v).hexdigest(), 'size': len(v)}
    if isinstance(v, int) and abs(v) > 2**53 - 1:
        return {'$integer': str(v)}
    if isinstance(v, float) and not math.isfinite(v):
        return {'$float': str(v)}
    if isinstance(v, str) and len(v) > MAX_CELL:
        return {'$text_preview': v[:MAX_CELL], 'characters': len(v), 'truncated': True}
    return v


def query(archive, sql, parameters=None, limit=100, offset=0):
    """Run SELECTs with an authorizer and a two-second VM budget.

    Long SQL cells are previews; retrieve full source payloads with get_record,
    blob bytes with get_blob, or explicit substr() queries for computed text.
    """
    bound(limit)
    if offset < 0:
        raise ValueError('offset must be nonnegative')
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    safe_functions = {'abs','avg','coalesce','count','date','datetime','glob','hex','ifnull','iif','instr','julianday','length','like','likelihood','likely','lower','ltrim','max','min','nullif','printf','quote','random','randomblob','replace','round','rtrim','sign','strftime','substr','substring','sum','time','total','trim','typeof','unicode','unixepoch','unlikely','upper','zeroblob','json','json_array','json_array_length','json_extract','json_insert','json_object','json_patch','json_quote','json_remove','json_replace','json_set','json_type','json_valid','json_group_array','json_group_object','row_number','rank','dense_rank','lag','lead','first_value','last_value','nth_value'}
    def authorize(action, a, b, db, trigger):
        if action not in allowed:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (b or '').lower() not in safe_functions:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    c = connect(archive)
    try:
        c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
        c.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 200_000)
        c.set_authorizer(authorize)
        deadline = time.monotonic() + 2
        c.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        cursor = c.execute('SELECT * FROM (' + sql.strip().rstrip(';') + ') LIMIT ? OFFSET ?', [*(parameters or []), limit + 1, offset])
        names = [x[0] for x in cursor.description]
        output = []
        budget = 200_000
        has_more = False
        for row in cursor:
            converted = [scalar(v) for v in row]
            size = len(json.dumps(converted))
            if size > 200_000:
                raise ValueError('One result row exceeds response budget; select fewer columns or substr() chunks')
            if len(output) == limit or size > budget:
                has_more = True
                break
            output.append(converted)
            budget -= size
        return {'columns': names, 'rows': output, 'offset': offset, 'next_offset': offset + len(output) if has_more else None}
    finally:
        c.close()



def get_record(archive, version_id, offset=0, limit=MAX_CELL):
    bound(limit, MAX_CELL)
    if offset < 0:
        raise ValueError('offset must be nonnegative')
    with closing(connect(archive)) as c:
        row = c.execute('SELECT * FROM records WHERE version_id=?', (version_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown version_id')
        result = dict(row)
        payload = result.pop('payload_json')
        result.update(payload_chunk=payload[offset:offset+limit], offset=offset, characters=len(payload), next_offset=offset+limit if offset+limit<len(payload) else None)
        if offset == 0 and len(payload)<=limit:
            result['payload'] = json.loads(payload)
        return result


def get_blob(archive, hash, offset=0, limit=8192):
    bound(limit, 32768)
    if offset < 0:
        raise ValueError('offset must be nonnegative')
    with closing(connect(archive)) as c:
        r = c.execute('SELECT size,substr(data,?,?) AS chunk FROM blobs WHERE hash=?', (offset+1,limit,hash)).fetchone()
        if r is None:
            raise ValueError('Unknown blob hash')
        return {'hash':hash,'size':r['size'],'offset':offset,'base64':base64.b64encode(r['chunk']).decode(), 'next_offset':offset+limit if offset+limit<r['size'] else None}



def decode_blob(archive, hash, offset=0, limit=12000):
    """Decode data formats only; NSKeyedArchiver stays an explicit UID graph.

    Never unpickle, resolve class names, or instantiate archived objects. Input,
    nesting and node bounds keep malformed metadata from exhausting the server.
    Original bytes remain independently available through get_blob.
    """
    import plistlib
    from xml.parsers.expat import ExpatError
    bound(limit, 12000)
    if offset < 0:
        raise ValueError('offset must be nonnegative')
    with closing(connect(archive)) as c:
        row = c.execute('SELECT size FROM blobs WHERE hash=?', (hash,)).fetchone()
        if row is None:
            raise ValueError('Unknown blob hash')
        if row['size'] > 2_000_000:
            return {'status':'unsupported','reason':'Decoder input exceeds 2 MB; use get_blob for all original bytes','hash':hash}
        data = c.execute('SELECT data FROM blobs WHERE hash=?', (hash,)).fetchone()['data']
    decoder = 'plist' if data.startswith(b'bplist00') or data.lstrip().startswith((b'<?xml',b'<plist')) else 'json'
    try:
        parsed = plistlib.loads(data) if decoder == 'plist' else json.loads(data.decode('utf-8'))
        remaining = 100_000
        def convert(value, depth=0):
            nonlocal remaining
            remaining -= 1
            if depth > 60 or remaining < 0:
                raise ValueError('Decoded data exceeds nesting or node budget')
            if isinstance(value, plistlib.UID):
                return {'$uid':value.data}
            if isinstance(value, bytes):
                return {'$bytes_base64':base64.b64encode(value).decode('ascii')}
            if isinstance(value, dt.datetime):
                # plist dates are UTC even when plistlib returns naive datetime.
                return {'$date':value.replace(tzinfo=dt.timezone.utc).isoformat()}
            if isinstance(value, dict):
                return {str(k):convert(v,depth+1) for k,v in value.items()}
            if isinstance(value, list):
                return [convert(v,depth+1) for v in value]
            if isinstance(value, float) and not math.isfinite(value):
                return {'$float':str(value)}
            if isinstance(value, int) and abs(value)>2**53-1:
                return {'$integer':str(value)}
            return value
        payload = json.dumps(convert(parsed),ensure_ascii=True,allow_nan=False)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError, ExpatError, plistlib.InvalidFileException) as exc:
        return {'status':'unsupported','decoder':decoder,'hash':hash,
                'reason':f'Cannot safely decode metadata: {type(exc).__name__}; original bytes remain available through get_blob'}
    return {'status':'decoded','decoder':decoder,'hash':hash,'decoded_chunk':payload[offset:offset+limit],
            'offset':offset,'characters':len(payload),'next_offset':offset+limit if offset+limit<len(payload) else None,
            'note':'NSKeyedArchiver UID references remain unresolved; source text is untrusted data'}


def parse_time(value):
    if value is None: return None
    try:
        t=datetime.fromisoformat(value.replace('Z','+00:00'))
        if t.utcoffset() is None: raise ValueError('Time filters require an explicit UTC offset or Z')
        return t.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    except (AttributeError,TypeError) as e:
        raise ValueError('Expected ISO timestamp with UTC offset') from e


def sources(archive, limit=100, offset=0):
    return query(archive,'SELECT * FROM sources ORDER BY id',limit=limit,offset=offset)


def status(archive, limit=100, offset=0):
    """Latest attempts and captures; stale means no successful capture in 15 minutes."""
    return query(archive,"""SELECT s.*,c.id AS capture_id,c.observed_at,c.row_count,
        CASE WHEN julianday('now')-julianday(c.observed_at)>15.0/1440 THEN 1 ELSE 0 END AS stale,
        a.status AS last_attempt_status,a.message AS last_attempt_message,a.attempted_at
        FROM sources s LEFT JOIN captures c ON c.id=(SELECT max(id) FROM captures WHERE source_id=s.id)
        LEFT JOIN attempts a ON a.id=(SELECT max(id) FROM attempts WHERE source_path=s.path)
        ORDER BY s.id""",limit=limit,offset=offset) | {
        'recent_issues': query(archive,"SELECT * FROM attempts WHERE status!='ok' ORDER BY id DESC",limit=20)['rows'],
        'note':'Successful capture is not proof of complete iCloud sync; discovery failures are listed separately.'}


def describe_schema(archive, offset=0, limit=60000):
    bound(limit,60000)
    if offset<0: raise ValueError('offset must be nonnegative')
    with closing(connect(archive)) as c:
        data={'archive':[dict(r) for r in c.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'")],
              'sources':[dict(r) for r in c.execute('SELECT source_id,schema_json FROM captures WHERE id IN (SELECT max(id) FROM captures GROUP BY source_id)')]}
    payload=canonical(data)
    return {'schema_chunk':payload[offset:offset+limit], 'next_offset':offset+limit if offset+limit<len(payload) else None,'characters':len(payload)}


def _filters(start=None,end=None,source_id=None,profile=None,kind=None,browser=None,domain=None,url=None,app_id=None,fidelity=None,provenance=None):
    start,end=parse_time(start),parse_time(end)
    if start and end and start>=end: raise ValueError('start must precede end')
    return dict(start=start,end=end,source_id=source_id,profile=profile,kind=kind,browser=browser,domain=domain,url=url,app_id=app_id,fidelity=fidelity,provenance=provenance)


def _scope(c, filters, search_text, cursor=None):
    info=c.execute('SELECT * FROM archive_info').fetchone()
    fingerprint=digest(canonical({'filters':filters,'search':search_text}))
    scope={'archive':info['archive_id'],'generation':info['index_generation'],'filter':fingerprint,
           'through':c.execute('SELECT coalesce(max(id),0) FROM captures').fetchone()[0], 'after':None}
    if cursor:
        if len(cursor)>4096: raise ValueError('Invalid cursor')
        try: decoded=json.loads(base64.urlsafe_b64decode(cursor).decode())
        except Exception as e: raise ValueError('Invalid cursor') from e
        if not isinstance(decoded,dict) or any(decoded.get(k)!=scope[k] for k in ('archive','generation','filter')):
            raise ValueError('Cursor belongs to another query/archive or obsolete index')
        through=decoded.get('through'); after=decoded.get('after')
        if type(through) is not int or not 0<=through<=scope['through'] or not isinstance(after,list) or len(after)!=2 or not isinstance(after[0],str) or type(after[1]) is not int:
            raise ValueError('Invalid cursor boundary')
        scope=decoded
    return scope


def _select(c, f, scope, search_text=None):
    # Indexed anti-join chooses the latest interpretation *at this observation boundary*.
    # Filtering follows revision selection, so an old title cannot resurrect a revised event.
    where=['e.capture_id<=?', 'NOT EXISTS (SELECT 1 FROM events newer WHERE newer.source_id=e.source_id AND newer.event_key=e.event_key AND newer.capture_id>e.capture_id AND newer.capture_id<=?)']
    args=[scope['through'],scope['through']]
    columns={'source_id':'e.source_id','profile':'s.profile','kind':'e.kind','browser':'s.browser','domain':'e.domain','url':'e.url','app_id':'e.app_id','fidelity':'s.fidelity','provenance':'c.provenance'}
    for k,column in columns.items():
        if f[k] is not None: where.append(column+'=?');args.append(f[k])
    if f['start']:
        where.append("((e.ended_at IS NULL AND e.started_at>=?) OR (e.ended_at IS NOT NULL AND e.ended_at>?) OR (e.ended_at=e.started_at AND e.started_at>=?))")
        args.extend([f['start']]*3)
    if f['end']: where.append('e.started_at<?');args.append(f['end'])
    if search_text is not None:
        words=search_text.split()
        if not words: where.append('0')
        else:
            # Literal token phrases, never user-provided FTS operators.
            match=' AND '.join('"'+w.replace('"','""')+'"' for w in words)
            where.append('e.id IN (SELECT rowid FROM event_search WHERE event_search MATCH ?)');args.append(match)
    if scope.get('after'):
        where.append("(coalesce(e.started_at,''),e.id)>(?,?)");args.extend(scope['after'])
    sql='''SELECT e.*,s.adapter,s.browser,s.profile,s.device,s.fidelity,c.provenance,c.observed_at,c.collected_by
           FROM events e JOIN sources s ON s.id=e.source_id JOIN captures c ON c.id=e.capture_id
           WHERE '''+' AND '.join(where)+" ORDER BY coalesce(e.started_at,''),e.id"
    return c.execute(sql,args)


def _event(row):
    payload=json.loads(row['payload_json'])
    event={k:row[k] for k in ('source_id','event_key','capture_id','kind','started_at','ended_at','url','title','domain','app_id','adapter','browser','profile','device','fidelity','provenance','observed_at','collected_by')}
    event.update(raw_refs=payload.get('raw_refs',[]),normalizer_version=payload.get('normalizer_version',1))
    if payload.get('historical_join_refs'):event['historical_join_refs']=payload['historical_join_refs']
    # Timeline is compact; arbitrary original fields remain available through get_record.
    truncated=[]
    for k,v in event.items():
        if isinstance(v,str) and len(v)>2048:
            event[k]=v[:2048];truncated.append(k)
    if truncated: event['truncated_fields']=truncated
    return event


def _coverage(c,through):
    return {'through_capture':through,
            'sources':[dict(r) for r in c.execute('SELECT source_id,max(observed_at) AS last_observed_at FROM captures WHERE id<=? GROUP BY source_id LIMIT 501',(through,))],
            'note':'Latest observed events, including pruned records. Sync completeness and attention are not established. Legacy browser observations may overlap native visits; filter fidelity or provenance for separate totals.'}


def _page(archive, filters, limit, cursor, search_text=None):
    bound(limit)
    with closing(connect(archive)) as c:
        c.execute('BEGIN')
        scope=_scope(c,filters,search_text,cursor)
        events=[];last=None;more=False;remaining=200_000
        for row in _select(c,filters,scope,search_text):
            event=_event(row);size=len(canonical(event))
            if len(events)==limit or size>remaining:
                more=True;break
            events.append(event);remaining-=size;last=[row['started_at'] or '',row['id']]
        if more and last is None: raise ValueError('Event exceeds response budget; use raw SQL and chunk retrieval')
        next_cursor=base64.urlsafe_b64encode(canonical({**scope,'after':last}).encode()).decode() if more else None
        return {'events':events,'next_cursor':next_cursor,'coverage':_coverage(c,scope['through'])}


def timeline(archive,start=None,end=None,source_id=None,profile=None,kind=None,browser=None,domain=None,url=None,app_id=None,fidelity=None,provenance=None,limit=100,cursor=None):
    return _page(archive,_filters(start,end,source_id,profile,kind,browser,domain,url,app_id,fidelity,provenance),limit,cursor)


def search(archive,text,start=None,end=None,source_id=None,profile=None,kind=None,browser=None,domain=None,url=None,app_id=None,fidelity=None,provenance=None,limit=100,cursor=None):
    if not isinstance(text,str) or len(text)>4096: raise ValueError('Search text must be at most 4096 characters')
    return _page(archive,_filters(start,end,source_id,profile,kind,browser,domain,url,app_id,fidelity,provenance),limit,cursor,text)


def _summary(archive,filters,mode,limit):
    bound(limit)
    with closing(connect(archive)) as c:
        c.execute('BEGIN');scope=_scope(c,filters,None)
        groups={}; total_events=0
        deadline=time.monotonic()+5
        for r in _select(c,filters,scope):
            if time.monotonic()>deadline: raise ValueError('Summary exceeded five-second budget; narrow the time or source filters')
            total_events+=1
            # Keep legacy and native totals distinct even if their apparent entities match.
            entity=r['app_id'] if mode=='apps' else r['domain']
            key=(entity,r['fidelity'])
            g=groups.setdefault(key,{'app_id' if mode=='apps' else 'domain':entity,'fidelity':r['fidelity'],'visits':0,'intervals':[],'evidence':[]})
            if len(g['evidence'])<5:g['evidence'].extend(json.loads(r['payload_json']).get('raw_refs',[])[:1])
            if mode=='domains':g['visits']+=1;continue
            s,t=r['started_at'],r['ended_at']
            if not s or not t or t<s:continue
            s=max(s,filters['start']) if filters['start'] else s
            t=min(t,filters['end']) if filters['end'] else t
            if t>=s:g['intervals'].append((s,t))
        output=[]
        for g in groups.values():
            spans=g.pop('intervals')
            if mode=='apps':
                g.pop('visits');g['intervals']=len(spans)
                seconds=lambda s,t:(datetime.fromisoformat(t.replace('Z','+00:00'))-datetime.fromisoformat(s.replace('Z','+00:00'))).total_seconds()
                g['recorded_seconds']=sum(seconds(s,t) for s,t in spans)
                merged=[]
                for s,t in sorted(spans):
                    if merged and s<=merged[-1][1]:merged[-1]=(merged[-1][0],max(t,merged[-1][1]))
                    else:merged.append((s,t))
                g['union_seconds']=sum(seconds(s,t) for s,t in merged)
            output.append(g)
        output.sort(key=lambda g:(-g['recorded_seconds'] if mode=='apps' else -g['visits'],str(g.get('app_id',g.get('domain'))),g['fidelity']))
        page=[];budget=150_000
        for g in output[:limit]:
            field='app_id' if mode=='apps' else 'domain'
            if isinstance(g[field],str) and len(g[field])>2048:
                g[field]=g[field][:2048];g['truncated_fields']=[field]
            size=len(canonical(g))
            if size>budget:break
            page.append(g);budget-=size
        return {mode:page,'groups_total':len(output),'truncated':len(output)>len(page),'events_considered':total_events,'coverage':_coverage(c,scope['through'])}


def app_usage_summary(archive,start=None,end=None,source_id=None,profile=None,browser=None,app_id=None,fidelity=None,provenance=None,limit=100):
    return _summary(archive,_filters(start,end,source_id,profile,'app_usage',browser,app_id=app_id,fidelity=fidelity,provenance=provenance),'apps',limit)


def domain_summary(archive,start=None,end=None,source_id=None,profile=None,browser=None,domain=None,fidelity=None,provenance=None,limit=100):
    return _summary(archive,_filters(start,end,source_id,profile,'browser_visit',browser,domain=domain,fidelity=fidelity,provenance=provenance),'domains',limit)

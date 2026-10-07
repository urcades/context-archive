"""Rebuildable, explained correlations. Matching rules are user assertions, not evidence."""
from contextlib import closing
from datetime import datetime
import base64
import json
import re
import time
from . import core, query

VERSION = 1
LIMITATIONS = ('Temporal proximity does not establish attention, causation, shared device, or website dwell time. '
              'Lanes distinguish source, collecting installation, and provenance; they are not inferred devices. '
              'Synced Safari and imported observations may overlap local activity. Episode span is not usage duration. '
              'Rules describe explicit user-authored associations, not verified project work. Window edges clip episodes.')


def _registry(c):
    # Optional additive metadata: older archives stay readable without a migration
    # or any write from an MCP request. Only the explicit CLI editor creates it.
    if not c.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='projects'").fetchone():
        return []
    return [json.loads(r[0]) for r in c.execute('SELECT definition_json FROM projects ORDER BY id')]


def put_project(archive, definition):
    """Replace one explicit project definition atomically; enabled=false disables it."""
    if not isinstance(definition, dict) or set(definition)-{'id','name','rules','enabled'}:
        raise ValueError('Project requires id, name, rules, and optional enabled')
    identifier=definition.get('id'); name=definition.get('name'); rules=definition.get('rules')
    if not isinstance(identifier,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',identifier):
        raise ValueError('Project id must be 1-64 lowercase letters, digits, underscores or hyphens')
    if not isinstance(name,str) or not 1<=len(name)<=200 or type(definition.get('enabled',True)) is not bool:
        raise ValueError('Project name must be 1-200 characters and enabled must be boolean')
    if not isinstance(rules,list) or not 1<=len(rules)<=32:
        raise ValueError('Project requires 1-32 rules')
    for rule in rules:
        if not isinstance(rule,dict) or set(rule)!={'field','operator','value'}:
            raise ValueError('Rule requires field, operator, value')
        allowed={'domain':{'exact'},'url':{'exact','prefix'},'app_id':{'exact'},'title':{'exact','contains'},'source_id':{'exact'},'profile':{'exact'}}
        if rule['field'] not in allowed or rule['operator'] not in allowed[rule['field']]:
            raise ValueError('Unsupported rule field/operator')
        if not isinstance(rule['value'],str) or not 1<=len(rule['value'])<=2048:
            raise ValueError('Rule value must be 1-2048 characters')
    value={**definition,'enabled':definition.get('enabled',True)}
    with core.writer(archive) as c:
        c.execute('CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, updated_at TEXT NOT NULL)')
        if c.execute('SELECT count(*) FROM projects WHERE id!=?',(identifier,)).fetchone()[0]>=100:
            raise ValueError('Registry supports at most 100 projects')
        c.execute('INSERT INTO projects VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET definition_json=excluded.definition_json, updated_at=excluded.updated_at',
                  (identifier,core.canonical(value),core.now()))
    return {'project':value}


def projects(archive):
    with closing(query.connect(archive)) as c:
        values=_registry(c)
    # Bound the entire registry response as well as its individual definitions.
    if len(core.canonical(values))>180_000:
        raise ValueError('Registry exceeds response budget; retrieve definitions with bounded SQL')
    return {'projects':values,'registry_revision':core.digest(core.canonical(values))}


def _matches(row, registry):
    matches=[]
    for project in registry:
        if not project['enabled']:continue
        indexes=[]
        for index,rule in enumerate(project['rules']):
            actual=row[rule['field']]; value=rule['value']; op=rule['operator']
            if not isinstance(actual,str):continue
            # Literal, case-sensitive rules: no regex execution or URL rewriting.
            # Prefix anchors respect a URL boundary, avoiding /repo-other matches.
            matched=(actual==value if op=='exact' else value in actual if op=='contains' else
                     actual.startswith(value) and (len(actual)==len(value) or value.endswith(('/', '?', '#', '&', '=')) or actual[len(value)] in '/?#'))
            if matched:indexes.append(index)
        if indexes:matches.append({'project_id':project['id'],'rule_indexes':indexes,'relationship':'explicit_rule_match'})
    return matches


def _seconds(a,b):
    return (datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()


def retrieve(archive, mode, start, end, source_id=None, profile=None, kind=None,
             fidelity=None, provenance=None, project_id=None, gap_seconds=300,
             max_span_seconds=3600, limit=20, cursor=None, compact=False):
    if type(compact) is not bool: raise ValueError("compact must be boolean")
    query.bound(limit,100)
    if type(gap_seconds) is not int or not 0<=gap_seconds<=3600:
        raise ValueError('gap_seconds must be an integer between 0 and 3600')
    if type(max_span_seconds) is not int or not 1<=max_span_seconds<=86400:
        raise ValueError('max_span_seconds must be an integer between 1 and 86400')
    filters=query._filters(start=start,end=end,source_id=source_id,profile=profile,kind=kind,fidelity=fidelity,provenance=provenance)
    if not filters['start'] or not filters['end'] or _seconds(filters['start'],filters['end'])>31*86400:
        raise ValueError('Explicit timezone-aware start/end within 31 days are required')
    with closing(query.connect(archive)) as c:
        c.execute('BEGIN')
        registry=_registry(c); revision=core.digest(core.canonical(registry))
        if project_id is not None and not any(p['id']==project_id and p['enabled'] for p in registry):
            raise ValueError('Unknown or disabled project')
        fingerprint={**filters,'mode':mode,'project_id':project_id,'registry_revision':revision,
                     'gap_seconds':gap_seconds,'max_span_seconds':max_span_seconds,'correlator_version':VERSION,'compact':compact}
        scope=query._scope(c,fingerprint,None,cursor)
        # Recompute the bounded window at the fixed observation boundary before
        # paginating episodes. Paginating raw events first would split episodes.
        rows=[]; deadline=time.monotonic()+5; scanned=0; input_bytes=0
        for row in query._select(c,filters,{**scope,'after':None}):
            scanned+=1;input_bytes+=len(row['payload_json'])
            if scanned>10000 or input_bytes>20_000_000 or time.monotonic()>deadline:
                raise ValueError('Correlation window exceeds budget; narrow time/source filters')
            matches=_matches(row,registry)
            if project_id is not None and not any(m['project_id']==project_id for m in matches):continue
            event=query._event(row)
            event['project_matches']=matches
            event['lane']={'source_id':row['source_id'],'collected_by':row['collected_by'],'provenance':row['provenance']}
            rows.append(event)
        if mode=='project_timeline':
            items=rows; key='events'
        else:
            items=[]; key='episodes'
            # Group by event onset, never extend an episode with app interval end:
            # one long/background interval must not bridge hours of unrelated visits.
            for event in rows:
                onset=max(event['started_at'],filters['start'])
                if (not items or _seconds(items[-1]['last_onset'],onset)>gap_seconds or
                        _seconds(items[-1]['started_at'],onset)>max_span_seconds):
                    items.append({'started_at':onset,'last_onset':onset,'relationship':'temporal_proximity','events':[]})
                episode=items[-1];episode['last_onset']=onset;episode['events'].append(event)
            for episode in items:
                episode['span_seconds']=_seconds(episode['started_at'],episode.pop('last_onset'))
                episode['event_count']=len(episode['events'])
                episode['lanes']=list({core.canonical(e['lane']):e['lane'] for e in episode['events']}.values())
                episode['id']=core.digest(core.canonical([scope['archive'],scope['through'],revision,episode['started_at'],[(e['source_id'],e['event_key']) for e in episode['events']]]))
        insight={}
        if compact and mode=='episodes':
            from .insights import compact_episode, recurring_domains, window_coverage
            insight={'recurring_domains':recurring_domains(items),
                     'window_coverage':window_coverage(c,rows,filters,scope['through']),
                     'interpretation_version':1,
                     'navigation_note':'Groups are presentation only, not deduplicated visits. Authentication labels are heuristics. No human-versus-agent attribution or task outcome is inferred.'}
            items=[compact_episode(item) for item in items]
        offset=scope['after'][1] if scope['after'] else 0
        if offset<0 or offset>len(items):raise ValueError('Invalid correlation cursor offset')
        page=[]; budget=160_000-len(core.canonical(insight))
        for item in items[offset:offset+limit]:
            size=len(core.canonical(item))
            if size>budget:
                if not page:raise ValueError('Correlation result exceeds response budget; narrow window or use project-timeline/timeline')
                break
            page.append(item);budget-=size
        next_offset=offset+len(page)
        token=base64.urlsafe_b64encode(core.canonical({**scope,'after':['offset',next_offset]}).encode()).decode() if next_offset<len(items) else None
        return {**insight,key:page,'next_cursor':token,'total':len(items),'events_scanned':scanned,
                'registry_revision':revision,'correlator_version':VERSION,
                'coverage':query._coverage(c,scope['through']),'limitations':LIMITATIONS}

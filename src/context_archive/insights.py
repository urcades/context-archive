"""Deterministic reading aids; never modify evidence or infer intent/device identity."""
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit
from . import core


def _reference(event):
    return {k:event[k] for k in ('source_id','event_key','raw_refs')}


def _navigation_type(url):
    # A hint, not a claim about the page's contents or the user's purpose.
    try:
        parts=urlsplit(url or '')
        if parts.hostname in {'accounts.google.com','login.microsoftonline.com','login.tailscale.com'}:
            return 'possible_authentication'
        if any(segment.lower() in {'login','signin','oauth','authorize'} for segment in parts.path.split('/')):
            return 'possible_authentication'
    except ValueError:
        pass
    return 'navigation'


def compact_episode(episode):
    groups=[]; latest={}; apps=Counter(); other=Counter()
    for event in episode['events']:
        if event['kind']!='browser_visit':
            if event['kind']=='app_usage':apps[event['app_id']]+=1
            else:other[event['kind']]+=1
            continue
        # Keep the entire URL (including query/fragment), profile, provenance and
        # installation. Similar URLs are not proof of an identical visit.
        lane=core.canonical(event['lane'])
        previous=latest.get(lane)
        elapsed=(datetime.fromisoformat(event['started_at'])-datetime.fromisoformat(previous['last_at'])).total_seconds() if previous else 0
        if previous and event['url'] and 'url' not in event.get('truncated_fields',[]) and not previous.get('url_truncated') and previous['url']==event['url'] and 0<=elapsed<=30:
            group=previous
            group['reason']='same_url_within_30_seconds_same_lane'
        else:
            group={k:event[k] for k in ('url','title','domain','browser','profile','lane')}
            group.update(first_at=event['started_at'],last_at=event['started_at'],visit_count=0,
                         reason='individual_visit',navigation_type=_navigation_type(event['url']),evidence=[])
            group['url_truncated']='url' in event.get('truncated_fields',[])
            groups.append(group);latest[lane]=group
        group['last_at']=event['started_at'];group['visit_count']+=1
        group['evidence'].append(_reference(event))
    result={k:v for k,v in episode.items() if k!='events'}
    result.update(navigation_groups=groups,app_interval_counts=dict(apps),other_event_counts=dict(other),
                  evidence=[_reference(e) for e in episode['events'] if e['kind']!='browser_visit'])
    return result


def recurring_domains(episodes):
    domains={}
    for episode in episodes:
        for domain in sorted({e['domain'] for e in episode['events'] if e['kind']=='browser_visit' and e['domain']}):
            domains.setdefault(domain,[]).append(episode['id'])
    values=[{'domain':domain,'episode_count':len(ids),'episode_ids':ids[:20],
             'episode_ids_truncated':len(ids)>20,'relationship':'domain_revisited_not_proof_of_shared_task'}
            for domain,ids in domains.items() if len(ids)>1]
    return sorted(values,key=lambda v:(-v['episode_count'],v['domain']))[:20]


def window_coverage(c,rows,filters,through):
    """Describe the selected evidence, not a claim of continuous collection.

    Capture IDs pin observation state. Failed attempts lack that boundary, so we
    deliberately do not mix live diagnostics into a reproducible historical view.
    """
    sources=[]
    clauses=[];args=[]
    for field in ('source_id','profile','fidelity'):
        if filters.get(field) is not None:
            clauses.append(('id' if field=='source_id' else field)+'=?');args.append(filters[field])
    sql='SELECT id,browser,profile,adapter FROM sources'
    if clauses:sql+=' WHERE '+' AND '.join(clauses)
    candidates=c.execute(sql+' ORDER BY id LIMIT 501',args).fetchall()
    by_source={}
    for e in rows:by_source.setdefault(e['source_id'],[]).append(e)
    for source in candidates[:500]:
        events=by_source.get(source['id'],[])
        capture=c.execute('SELECT max(observed_at) FROM captures WHERE source_id=? AND id<=?',(source['id'],through)).fetchone()[0]
        # Sources first discovered after the fixed boundary must not shift pages.
        if capture is None:continue
        sources.append({**dict(source),'event_count':len(events),
            'first_event_at':min((e['started_at'] for e in events),default=None),
            'last_event_at':max((e['started_at'] for e in events),default=None),
            'last_successful_observation_at':capture,
            'observed_after_window':sum(e['observed_at']>=filters['end'] for e in events),
            'best_effort_events':sum(e['provenance']=='native_best_effort_copy' for e in events),
            'evidence_state':'events_present' if events else 'no_matching_events_not_proof_of_inactivity'})
    return {'start':filters['start'],'end':filters['end'],'sources':sources,'sources_truncated':len(candidates)>500,
            'note':'Filtered evidence only. Observation time is not activity time; later observation does not prove sync delay. '
                   'Event gaps do not prove inactivity. Historical permission/lock status is unknown here; use control-status for current diagnostics.'}

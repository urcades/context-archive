from context_archive import query
from test_correlation import fixture, START, END


def test_compact_groups_preserve_lanes_and_evidence(tmp_path):
    a=fixture(tmp_path)
    result=query.episodes(a,start=START,end=END,compact=True)
    first=result['episodes'][0]
    assert 'events' not in first
    assert first['event_count']==4
    assert len(first['navigation_groups'])==2
    assert all(g['visit_count']==2 and len(g['evidence'])==2 for g in first['navigation_groups'])
    assert first['navigation_groups'][0]['reason']=='same_url_within_30_seconds_same_lane'
    assert result['recurring_domains'][0]['episode_count']==2
    assert result['window_coverage']['sources'][0]['event_count']==3
    assert result['window_coverage']['sources'][0]['observed_after_window']==3


def test_compact_cursor_is_bound_to_view(tmp_path):
    import pytest
    a=fixture(tmp_path)
    first=query.episodes(a,start=START,end=END,compact=True,limit=1)
    second=query.episodes(a,start=START,end=END,compact=True,limit=1,cursor=first['next_cursor'])
    assert len(second['episodes'])==1
    assert second['recurring_domains']==first['recurring_domains']
    with pytest.raises(ValueError):
        query.episodes(a,start=START,end=END,cursor=first['next_cursor'])


def test_grouping_does_not_merge_query_parameters_or_tab_observations():
    from context_archive.insights import compact_episode
    base=dict(source_id='s',collected_by='m',provenance='native',profile='one',browser='Safari',
              lane={'source_id':'s'},kind='browser_visit',title='Example',domain='example.com',
              started_at=START,ended_at=None,raw_refs=[1],event_key='a',app_id=None)
    events=[dict(base,url='https://example.com/?q=one'),
            dict(base,url='https://example.com/?q=two',event_key='b'),
            dict(base,url='https://example.com/?q=two',event_key='c',kind='browser_tab_observation')]
    result=compact_episode({'events':events,'event_count':3})
    assert len(result['navigation_groups'])==2
    assert result['other_event_counts']=={'browser_tab_observation':1}


def test_empty_coverage_and_compact_validation(tmp_path):
    import pytest
    a=fixture(tmp_path)
    result=query.episodes(a,start='2002-01-01T00:00:00Z',end='2002-01-02T00:00:00Z',compact=True)
    assert result['episodes']==[]
    assert all(s['evidence_state']=='no_matching_events_not_proof_of_inactivity' for s in result['window_coverage']['sources'])
    with pytest.raises(ValueError):query.episodes(a,start=START,end=END,compact='yes')

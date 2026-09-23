"""Regression for the bounded discovery capacity."""

import hashlib
import json

from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.connectors.public_http import Response
from agent_research_intelligence.governance.policy import ClassifiedText, canonical
from agent_research_intelligence.research.discovery import DiscoveryResearch
from agent_research_intelligence.research.source_intelligence import source_id


def test_full_historical_registry_does_not_starve_new_search(workspace, monkeypatch):
    import agent_research_intelligence.research.discovery as mod

    for i in range(300):
        url = f'https://github.com/example/history-{i:03d}'
        workspace.write(
            f'data/sources/{source_id(url)}/old.json',
            canonical({'url': url, 'kind': 'github', 'title': 'Old unrelated item',
                       'summary': 'Other topic', 'last_verified': None, 'history_utility': []}),
        )

    class Documents:
        def search_github(self, query, count):
            assert isinstance(query, ClassifiedText) and count == 10
            return Response('https://api.github.com/search/repositories', 200, b'fixture', {}), [
                {'url': 'https://github.com/example/history-299', 'kind': 'github',
                 'title': 'Historical result outside shortlist', 'summary': 'Already known'},
                {'url': 'https://github.com/example/new-research-agent', 'kind': 'github',
                 'title': 'Research agent provenance', 'summary': 'Evidence provenance'}]

        def fetch(self, url, kind=None):
            return {'url': url, 'kind': kind, 'title': 'Research agent provenance',
                    'lines': ['Research agent evidence provenance'], 'locator': url,
                    'version': 'fixture', 'canonical': url, 'raw': b'fixture', 'links': []}

    brief = {'classification': 'PUBLIC',
             'queries': [{'provider': 'github', 'text': 'research agent provenance'}],
             'local_terms': ['research', 'provenance']}
    approval = {'authority': 'USER_AUTHORIZED_PUBLIC_RESEARCH',
                'brief_sha256': hashlib.sha256(canonical(brief)).hexdigest(),
                'source_message': 'FIXTURE_ONLY'}
    for name, value in [('intake', {'original_question': 'Synthetic evidence review'}),
                        ('brief', brief), ('approval', approval), ('seeds', [])]:
        workspace.write(name + '.json', canonical(value))

    result = DiscoveryResearch(workspace, Documents()).run(
        'intake.json', 'brief.json', 'approval.json', 'seeds.json')
    prefix = 'data/discovery/' + result['job'] + '/'
    records = json.loads(workspace.read(prefix + 'sources.json'))
    trace = json.loads(workspace.read(prefix + 'discovery-trace.json'))
    new = [r for r in records if r['url'] == 'https://github.com/example/new-research-agent']

    assert len(records) <= 300
    assert result['new_candidates'] == 1
    assert len(new) == 1 and new[0]['discovery_origin'] == 'TOOL_SEARCH'
    assert any(e['event'] == 'REGISTRY_JOB_BUDGET' for e in trace)
    assert any(e['event'] == 'ALREADY_KNOWN_URL' and e.get('basis') == 'PERSISTENT_REGISTRY_NOT_IN_JOB_SHORTLIST' for e in trace)
    assert sum(1 for r in records if r['discovery_origin'] == 'REUSED_SOURCE_REGISTRY') == 260
    assert len(list((workspace.root / 'data/sources').iterdir())) >= 300


def test_sufficient_known_coverage_keeps_full_historical_budget(workspace, monkeypatch):
    import agent_research_intelligence.research.discovery as mod

    for i in range(300):
        url = f'https://example.org/source-{i:03d}'
        kind = ['github', 'paper', 'web'][i % 3]
        workspace.write(
            f'data/sources/{source_id(url)}/old.json',
            canonical({'url': url, 'kind': kind, 'title': 'Research provenance',
                       'summary': 'Evidence provenance',
                       'last_verified': '2026-09-19T00:00:00+00:00',
                       'cross_validation': 'INDEPENDENT_SUPPORT_REVIEWED',
                       'history_utility': []}),
        )

    class Documents:
        def search_github(self, query, count):
            raise AssertionError('Known coverage should avoid external search')

        def fetch(self, url, kind=None):
            raise AcquisitionError('OFFLINE_FIXTURE')

    brief = {'classification': 'PUBLIC',
             'queries': [{'provider': 'github', 'text': 'research provenance'}],
             'local_terms': ['research', 'provenance']}
    approval = {'authority': 'USER_AUTHORIZED_PUBLIC_RESEARCH',
                'brief_sha256': hashlib.sha256(canonical(brief)).hexdigest(),
                'source_message': 'FIXTURE_ONLY'}
    for name, value in [('intake', {'original_question': 'Synthetic evidence review'}),
                        ('brief', brief), ('approval', approval), ('seeds', [])]:
        workspace.write(name + '.json', canonical(value))

    result = DiscoveryResearch(workspace, Documents()).run(
        'intake.json', 'brief.json', 'approval.json', 'seeds.json')
    prefix = 'data/discovery/' + result['job'] + '/'
    records = json.loads(workspace.read(prefix + 'sources.json'))
    trace = json.loads(workspace.read(prefix + 'discovery-trace.json'))
    assert len(records) == 300 and result['new_candidates'] == 0
    assert not any(e['event'] in ('TOOL_SEARCH', 'REGISTRY_JOB_BUDGET') for e in trace)

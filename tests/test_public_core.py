"""Synthetic checks; no account, model, media or network access."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_research_intelligence.cli.main import main
from agent_research_intelligence.governance.paths import BoundaryError, Workspace
from agent_research_intelligence.governance.policy import POLICY, canonical, verify_policy
from agent_research_intelligence.research.catalog import Catalog
from agent_research_intelligence.research.dossier import build, conflict_case
from agent_research_intelligence.research.discovery import code_hashes
from agent_research_intelligence.storage.database import Store
from agent_research_intelligence.watch.service import freeze_guard


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path.resolve() / 'workspace'
    assert main(['--root', str(root), 'init-workspace']) == 0
    ws = Workspace(root)
    verify_policy(ws)
    return ws


def test_policy_and_cli_smoke(workspace, capsys):
    assert json.loads(workspace.read('config/policies/safety.json')) == POLICY
    assert main(['--root', str(workspace.root), 'status']) == 0
    assert main(['--root', str(workspace.root), 'contract-list']) == 0
    assert 'ResearchQuestionCreate' in capsys.readouterr().out


def test_installed_workspace_keeps_code_provenance(workspace):
    hashes = code_hashes(workspace)
    assert 'research/discovery.py' in hashes
    assert len(hashes['research/discovery.py']) == 64


def test_catalog_dossier_and_provenance(workspace):
    catalog = Catalog(workspace)
    artifact = 'data/fixtures/source.txt'
    text = b'Synthetic evidence about memory architecture.'
    workspace.write(artifact, text)
    basis = {'artifact': artifact, 'sha256': hashlib.sha256(text).hexdigest(),
             'locator': 'line 1', 'classification': 'FIXTURE'}
    rq = catalog.put('question', {'problem': 'How should long-running agents retain evidence?',
        'current_context': '', 'constraints': [], 'need_to_learn': ['evidence retention'],
        'search_terms': ['memory']}, reason='Synthetic question')
    source = catalog.put('source', {'url': 'https://example.org/spec', 'entity_name': 'Example',
        'platform': 'web', 'kind': 'web', 'source_provenance': 'DEVELOPMENT_SEED',
        'basis': [basis]}, reason='Synthetic source')
    evidence = catalog.put('evidence', {'rq_id': rq['id'], 'source_id': source['id'],
        'source_revision': source['revision'], 'published_at': None,
        'captured_at': '2026-01-01T00:00:00Z', 'claim': 'Synthetic retention claim',
        'locator': {'url': 'https://example.org/spec', 'line': 1},
        'evidence_type': 'SOURCE_ASSERTION', 'source_quality': {}, 'relevance': {},
        'novelty': {'status': 'UNASSESSED'}, 'impact': {'status': 'UNASSESSED'},
        'confidence': 'UNKNOWN', 'limitations': ['Fixture only'],
        'provenance': [basis], 'classification': 'FIXTURE'}, reason='Synthetic evidence')
    dossier = build(workspace, rq['id'], evidence_ids=[evidence['id']], source_ids=[source['id']])
    assert dossier['payload']['Evidence Summary'][0]['id'] == evidence['id']
    assert 'Do not implement automatically' in workspace.read(dossier['artifact']).decode()
    assert Catalog(workspace).read(evidence['id'])['payload']['classification'] == 'FIXTURE'


def test_exclusive_store_lease_and_resource_policy(workspace):
    with Store(workspace):
        with pytest.raises((OSError, BoundaryError)):
            Store(workspace)
    with Store(workspace) as store:
        assert store.verify_audit() >= 0
    assert POLICY['limits']['free_disk_min_gib'] == 20
    assert POLICY['limits']['free_memory_min_gib'] == 6


def test_conflict_and_no_control():
    case = conflict_case({'claim': 'A', 'supporting_evidence': ['x'], 'limitations': ['scope']},
        {'claim': 'B', 'supporting_evidence': ['y'], 'limitations': ['scope']},
        hypothesis='Different inputs', experiment='Compare matched inputs')
    assert case['status'] == 'UNRESOLVED'
    p3 = freeze_guard('P3')
    assert p3['signal'] == 'BLOCKING_REVIEW_REQUIRED'
    assert p3['production_effects'] == []
    assert p3['control_capabilities'] == []


def test_question_requires_valid_record(workspace):
    with pytest.raises(Exception):
        Catalog(workspace).put('question', {'problem': 'short'}, reason='Invalid')


def test_initial_reads_keep_catalog_serial_and_account_independent(workspace):
    from threading import Event
    from agent_research_intelligence.research.initial_reads import run
    account_started = Event()
    catalog_started = Event()
    order = []
    class CatalogSpy:
        def read(self, identifier):
            assert account_started.wait(1)
            order.append('read')
            catalog_started.set()
            return {'id': identifier, 'payload': {'search_terms': ['memory']}}
        def search(self, kind, terms):
            order.append(kind)
            return []
    def status():
        account_started.set()
        assert catalog_started.wait(1)
        return {'status': 'AUTH_REQUIRED'}
    result = run(workspace, 'synthetic-question', catalog=CatalogSpy(), account_status=status)
    assert order == ['read', 'evidence', 'dossier', 'source']
    assert result['youtube_account']['status'] == 'AUTH_REQUIRED'


def test_discovery_fixture_builds_located_dossier(workspace):
    from agent_research_intelligence.connectors.public_http import Response
    from agent_research_intelligence.research.discovery import DiscoveryResearch

    class Documents:
        def __init__(self):
            self.kinds = ('github', 'paper', 'web')
        def _search(self, kind):
            url = 'https://example.org/' + kind
            return Response(url, 200, b'{"fixture":true}', {}), [{
                'url': url, 'kind': kind, 'title': 'Agent memory ' + kind,
                'summary': 'Agent memory design', 'discovery_locator': url,
            }]
        def search_github(self, *_): return self._search('github')
        def search_papers(self, *_): return self._search('paper')
        def search_crossref(self, *_): return self._search('web')
        def fetch(self, url, kind=None):
            return {'url': url, 'requested_url': url, 'canonical': url, 'kind': kind,
                    'title': 'Agent memory source', 'version': hashlib.sha256(url.encode()).hexdigest(),
                    'locator': url, 'lines': ['Agent memory design has a synthetic limitation.'],
                    'links': [], 'raw': ('Agent memory design: ' + url).encode()}

    brief = {'classification': 'PUBLIC', 'queries': [
        {'provider': 'github', 'text': 'agent memory'},
        {'provider': 'arxiv', 'text': 'agent memory'},
        {'provider': 'crossref', 'text': 'agent memory'},
    ], 'local_terms': ['agent', 'memory']}
    approval = {'authority': 'USER_AUTHORIZED_PUBLIC_RESEARCH',
                'brief_sha256': hashlib.sha256(canonical(brief)).hexdigest(),
                'source_message': 'Synthetic explicit public query'}
    for name, value in [('intake', {'original_question': 'How can agents retain memory?'}),
                        ('brief', brief), ('approval', approval), ('seeds', [])]:
        workspace.write('data/fixture-request/' + name + '.json', canonical(value))
    result = DiscoveryResearch(workspace, documents=Documents()).run(*(
        'data/fixture-request/' + name + '.json' for name in ('intake', 'brief', 'approval', 'seeds')))
    assert result['evidence_count'] >= 3
    assert set(result['evidence_types']) == {'github', 'paper', 'web'}
    assert workspace.read(result['dossier']).startswith(b'#')
    trace = json.loads(workspace.read('data/discovery/' + result['job'] + '/discovery-trace.json'))
    assert sum(item.get('event') == 'TOOL_SEARCH' for item in trace) == 3

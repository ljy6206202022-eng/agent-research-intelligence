import hashlib
import json
import pytest
from agent_research_intelligence.research.catalog import Catalog
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.storage.database import Store


def basis(ws,name='one'):
    path='data/fixtures/'+name+'.txt';ws.write(path,name.encode())
    return dict(artifact=path,sha256=hashlib.sha256(name.encode()).hexdigest(),locator='whole file',classification='FIXTURE')


def test_version_conflict_and_immutable_history(workspace):
    c=Catalog(workspace)
    q=dict(problem='How do agent handoffs preserve constraints?',current_context='Fixture only',constraints=[],need_to_learn=['handoff'],search_terms=['handoff'])
    first=c.put('question',q,reason='fixture question')
    second=c.put('question',{**q,'status':'RESEARCHING'},identifier=first['id'],expected_revision=1,reason='begin')
    assert c.read(first['id'],1)==first and c.read(first['id'])==second
    with pytest.raises(BoundaryError,match='Revision conflict'):
        c.put('question',q,identifier=first['id'],expected_revision=1,reason='stale')
    with Store(workspace) as store:assert store.verify_audit()==2


def test_source_lifecycle_not_account_mutation(workspace):
    c=Catalog(workspace);b=basis(workspace)
    s=dict(url='https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',entity_name='fixture',platform='youtube',kind='channel',source_provenance='USER_ADDED',basis=[b])
    row=c.put('source',s,reason='fixture')
    assert row['payload']['validation_count'] is None and row['payload']['historical_utility'] is None
    with pytest.raises(BoundaryError,match='transition'):
        c.put('source',{**s,'status':'ACTIVE'},identifier=row['id'],expected_revision=1,reason='illegal')
    with pytest.raises(BoundaryError,match='authenticated'):
        c.put('source',{**s,'subscription_state':'SUBSCRIBED'},reason='not observed')
    b2=basis(workspace,'two')
    nextrow=c.put('source',{**s,'status':'CANDIDATE','basis':[b,b2]},identifier=row['id'],expected_revision=1,reason='screened')
    assert nextrow['payload']['trust_level']=='UNKNOWN'
    assert not (workspace.root/'secrets').exists()


def test_basis_hash_and_secrets_fail_closed(workspace):
    c=Catalog(workspace);b=basis(workspace)
    from agent_research_intelligence.research.catalog import Basis
    with pytest.raises(BoundaryError):c.validate_basis([Basis(**{**b,'sha256':'a'*64})])
    with pytest.raises(BoundaryError):c.validate_basis([Basis(**{**b,'artifact':'secrets/token.json'})])


def test_fixture_cannot_be_real_evidence(workspace):
    from agent_research_intelligence.research.catalog import Evidence
    b=basis(workspace)
    with pytest.raises(ValueError,match='Fixture cannot'):
        Evidence(rq_id='q',source_id='s',source_revision=1,published_at=None,captured_at='now',claim='a',
                 locator={'line':1},evidence_type='SOURCE_ASSERTION',source_quality={},relevance={},novelty={},impact={},
                 confidence='UNKNOWN',limitations=['fixture'],provenance=[b],classification='REAL')

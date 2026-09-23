import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
from agent_research_intelligence.watch.service import Watch, digest, freeze_guard, entries
from agent_research_intelligence.connectors.public_http import PublicHTTP, Response, AcquisitionError
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.storage.database import Store

JOB='a'*32
BASE='data/discovery/'+JOB
TEXT='Task handoffs preserve constraints and unfinished work using references.'

def feed(items):
    return ('<rss><channel>'+''.join('<item><guid>'+key+'</guid><title>Agent context</title><description>'+text+'</description><link>https://example.org/'+key+'</link></item>' for key,text in items)+'</channel></rss>').encode()

class Clock:
    def __init__(self):self.value=datetime(2026,9,20,tzinfo=timezone.utc)
    def __call__(self):return self.value
    def advance(self,seconds=7200):self.value+=timedelta(seconds=seconds)

class HTTP:
    def __init__(self,*responses):self.responses=list(responses);self.trace=[];self.calls=[]
    def conditional_get(self,url,**headers):
        self.calls.append((url,headers));self.trace.append({'url':url,'method':'GET'})
        x=self.responses.pop(0)
        if isinstance(x,BaseException):raise x
        return x

def response(body,**headers):return Response('https://example.org/feed',200,body,headers)

def setup(workspace,kind='feed'):
    raw=feed([('old',TEXT)]) if kind=='feed' else TEXT.encode()
    url='https://example.org/feed' if kind=='feed' else 'https://example.org/page'
    workspace.write(BASE+'/sources.json',canonical([{'id':'source','url':url}]))
    workspace.write(BASE+'/question.json',canonical({'question':'Synthetic objective','classification':'PRIVATE'}))
    doc={'url':url,'version':digest(raw),'lines':[TEXT],'kind':kind}
    workspace.write(BASE+'/source.document.json',canonical(doc));workspace.write(BASE+'/source.raw',raw)
    workspace.write(BASE+'/reviews/r/dossier.md',b'Original synthetic dossier, immutable.')
    evidence=[{'id':'E1','document':BASE+'/source.document.json','document_sha256':digest(canonical(doc)),'lines':[1,1]}]
    workspace.write(BASE+'/reviews/r/evidence.json',canonical(evidence))
    config={'job':JOB,'dossier':BASE+'/reviews/r/dossier.md','local_terms':['context','handoff'],
        'targets':[{'source_id':'source','kind':kind,'document':BASE+'/source.document.json','raw':BASE+'/source.raw'}]}
    workspace.write('config/watch.json',canonical(config))
    return raw,config

def watch(workspace,http=None,clock=None,hook=None):return Watch(workspace,http=http or HTTP(),clock=clock or Clock(),guard=lambda:None,hook=hook,evidence_mode="FIXTURE")

def reviewed(workspace,w,wid,eid,novelty='HIGH',level='P2'):
    ep=w.prefix(wid)+'/events/'+eid+'.json';event=w.load(ep)
    v={'event_sha256':digest(workspace.read(ep)),'origin':'FIXTURE','reason':'Synthetic changed evidence about current handoff assumption, no actual research finding.',
       'relevance':'HIGH','novelty':novelty,'impact':level,'impact_score':.9 if level in ('P2','P3') else .2,
       'affected':['dossier assumption'],'lines':[1,len(event['text'].splitlines())],'finding':'FIXTURE: tested artifact references expire; new limitation.','duplicate_refs':['E1']}
    path='reviews/'+eid+'.json';workspace.write(path,canonical(v));return path

def counts(ws):
    with Store(ws) as s:return dict(s.db.execute('SELECT kind,count(*) FROM records GROUP BY kind'))

def test_no_change_hash_then_304_never_adds_evidence(workspace):
    raw,_=setup(workspace);h=HTTP(response(raw,etag='"one"'),Response('https://example.org/feed',304,b'',{}));w=watch(workspace,h);wid=w.register('config/watch.json')['watch_id'];old=workspace.read(BASE+'/reviews/r/dossier.md')
    a=w.poll(wid,force=True);b=w.poll(wid,force=True)
    assert a['outcomes'][0]['status']==b['outcomes'][0]['status']=='NO_CHANGE'
    assert h.calls[1][1]['etag']=='"one"'
    for result in (a,b):
        assert result['cost']['model_calls']==result['cost']['tokens']==result['cost']['deep_analysis_calls']==0
        assert result['cost']['new_evidence']==result['cost']['deterministic_content_screens']==0
    assert counts(workspace)=={} and workspace.read(BASE+'/reviews/r/dossier.md')==old

def test_relevant_duplicate_and_paraphrase_review_remain_distinct(workspace):
    setup(workspace);h=HTTP(response(feed([('old',TEXT),('new',TEXT)])),response(feed([('old',TEXT),('new',TEXT),('paraphrase','Task handoffs should retain unfinished obligations through linked artifacts.')])));w=watch(workspace,h);wid=w.register('config/watch.json')['watch_id']
    a=w.poll(wid,force=True);eid=a['outcomes'][0]['events'][0];event=w.load(w.prefix(wid)+'/events/'+eid+'.json')
    assert event['disposition']=='RELEVANT_DUPLICATE' and event['novelty']['existing_evidence']
    b=w.poll(wid,force=True);eid=b['outcomes'][0]['events'][0];event=w.load(w.prefix(wid)+'/events/'+eid+'.json')
    assert event['novelty']['status']=='UNASSESSED'
    result=w.review(wid,eid,reviewed(workspace,w,wid,eid,'DUPLICATE','P0'))
    assert result['new_evidence']==0 and result['rq_state']=='WATCHING' and counts(workspace)=={}

def test_novel_p2_revision_keeps_parent_and_replay_is_idempotent(workspace):
    setup(workspace);h=HTTP(response(feed([('old',TEXT),('new','Agent context references expire under a newly measured retention condition.')])));w=watch(workspace,h);wid=w.register('config/watch.json')['watch_id'];original=workspace.read(BASE+'/reviews/r/dossier.md')
    event=w.poll(wid,force=True)['outcomes'][0]['events'][0];rp=reviewed(workspace,w,wid,event);first=w.review(wid,event,rp);second=w.review(wid,event,rp)
    assert first==second and first['rq_state']=='REOPENED' and first['new_evidence']==1
    assert counts(workspace)=={'dossier':1,'evidence':1}
    assert workspace.read(BASE+'/reviews/r/dossier.md')==original
    assert workspace.read(first['dossier_revision']).startswith(original)
    assert w.state(wid)['rq_status']=='REOPENED'

@pytest.mark.parametrize('level,signal',[('P0','RECORD'),('P1','VNEXT_REVIEW'),('P2','GATE_REVIEW_REQUIRED'),('P3','BLOCKING_REVIEW_REQUIRED')])
def test_freeze_no_control(level,signal):
    g=freeze_guard(level);assert g['signal']==signal and g['control_capabilities']==g['production_effects']==[] and not g['auto_implementation']

@pytest.mark.parametrize('level,reopen',[('P0',False),('P1',False),('P2',True),('P3',True)])
def test_impact_integrated_records_internal_signal_only(workspace,level,reopen):
    setup(workspace);w=watch(workspace,HTTP(response(feed([('old',TEXT),('new','Agent context newly measured unsafe assumption fails.')]))));wid=w.register('config/watch.json')['watch_id'];eid=w.poll(wid,force=True)['outcomes'][0]['events'][0];result=w.review(wid,eid,reviewed(workspace,w,wid,eid,level=level))
    assert (result['rq_state']=='REOPENED')==reopen and result['impact']['production_effects']==[]
    assert result['new_evidence']==1

@pytest.mark.parametrize('failure',[AcquisitionError('RATE_LIMITED'),AcquisitionError('TIMEOUT'),response(b'<broken')])
def test_failure_does_not_consume_cursor_and_backoff_then_recovery(workspace,failure):
    raw,_=setup(workspace);clock=Clock();h=HTTP(failure,response(raw));w=watch(workspace,h,clock);wid=w.register('config/watch.json')['watch_id'];baseline=w.state(wid)['targets']['source']
    result=w.poll(wid,force=True);after=w.state(wid)['targets']['source']
    assert result['outcomes'][0]['status']=='FAILED_NOT_CONSUMED'
    assert after['raw_hash']==baseline['raw_hash'] and after['seen']==baseline['seen'] and after['etag']==baseline['etag']
    assert w.poll(wid,force=True)['outcomes'][0]['status']=='RETRY_NOT_DUE'
    clock.advance();assert w.poll(wid,force=True)['outcomes'][0]['status']=='NO_CHANGE'

@pytest.mark.parametrize('phase',['after_event_saved','before_cursor_commit'])
def test_actual_process_crash_before_cursor_replays_without_loss(workspace,phase):
    setup(workspace);w=watch(workspace);wid=w.register('config/watch.json')['watch_id'];old=w.state(wid)
    body=feed([('old',TEXT),('new','Agent context genuinely new limitation in fixture.')]);workspace.write('fixture-response.xml',body)
    code='''import os,sys
from pathlib import Path
from agent_research_intelligence.watch.service import Watch
from agent_research_intelligence.governance.paths import Workspace
from agent_research_intelligence.connectors.public_http import Response
class HTTP:
 trace=[]
 def conditional_get(self,url,**kw):return Response(url,200,(Path(sys.argv[1])/'fixture-response.xml').read_bytes(),{})
def hook(phase):
 if phase==sys.argv[3]:os._exit(73)
w=Watch(Workspace(Path(sys.argv[1])),http=HTTP(),guard=lambda:None,hook=hook,evidence_mode="FIXTURE")
w.poll(sys.argv[2],force=True)
'''
    result=subprocess.run([sys.executable,'-B','-c',code,str(workspace.root),wid,phase],env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')},capture_output=True,timeout=15)
    assert result.returncode==73, result.stderr
    assert w.state(wid)==old
    recovered=watch(workspace,HTTP(response(body))).poll(wid,force=True)
    assert len(recovered['outcomes'][0]['events'])==1
    assert len(w.state(wid)['processed_events'])==1
    assert len(list((workspace.root/w.prefix(wid)/'events').glob('*.json')))==1

def test_crash_after_evidence_before_cursor_does_not_duplicate_records(workspace):
    setup(workspace);w=watch(workspace,HTTP(response(feed([('old',TEXT),('new','Agent context new evidence.')]))));wid=w.register('config/watch.json')['watch_id'];eid=w.poll(wid,force=True)['outcomes'][0]['events'][0];rp=reviewed(workspace,w,wid,eid)
    class Crash(BaseException):pass
    def hook(phase):
        if phase=='after_review_artifacts':raise Crash()
    w.hook=hook
    with pytest.raises(Crash):w.review(wid,eid,rp)
    assert counts(workspace)=={'dossier':1,'evidence':1} and eid not in w.state(wid)['reviewed_events']
    w.hook=lambda _:None;w.review(wid,eid,rp);assert counts(workspace)=={'dossier':1,'evidence':1}

def test_duplicate_delivery_stop_resume_and_deadline(workspace):
    setup(workspace);body=feed([('old',TEXT),('new','Agent context new limitation.')]);clock=Clock();h=HTTP(response(body),response(body));w=watch(workspace,h,clock);wid=w.register('config/watch.json')['watch_id'];w.poll(wid,force=True)
    seen=w.state(wid)['targets']['source']['seen'];w.control(wid,'stop');assert w.poll(wid)['outcomes'][0]['status']=='DORMANT';assert len(h.calls)==1
    w.control(wid,'resume');assert w.poll(wid,force=True)['cost']['new_source_items']==0
    assert w.state(wid)['targets']['source']['seen']==seen
    clock.advance(91*86400);assert w.poll(wid)['outcomes'][0]['status']=='EXPIRED'
    with pytest.raises(BoundaryError):w.control(wid,'resume')

def test_feed_window_gap_is_explicit_and_not_claimed_complete(workspace):
    setup(workspace);w=watch(workspace,HTTP(response(feed([('new','Unrelated weather update.')]))));wid=w.register('config/watch.json')['watch_id'];result=w.poll(wid,force=True)
    assert result['outcomes'][0]['coverage']=='POSSIBLE_FEED_WINDOW_GAP'
    eid=result['outcomes'][0]['events'][0];ev=w.load(w.prefix(wid)+'/events/'+eid+'.json')
    assert ev['relevance']['status']=='CANDIDATE'  # feed title is explicitly about agent context
    rp=reviewed(workspace,w,wid,eid,'UNKNOWN','P0');v=json.loads(workspace.read(rp));v['relevance']='LOW';workspace.write(rp,canonical(v),replace=True)
    assert w.review(wid,eid,rp)['new_evidence']==0

def test_local_or_credential_urls_denied_before_network():
    calls=[]
    def resolver(*args,**kw):calls.append(args);return [(None,None,None,None,('127.0.0.1',443))]
    http=PublicHTTP(resolver=resolver)
    for url in ['https://127.0.0.1/','https://user:pw@example.org/','http://example.org/']:
        with pytest.raises(BoundaryError):http.conditional_get(url,etag='"x"')
    with pytest.raises(BoundaryError):http.conditional_get('https://example.org/',etag='x\r\nCookie: secret')

def test_github_sha_metadata_and_invalid_parser():
    obj={'sha':'a'*40,'commit':{'message':'Context handoff correction','committer':{'date':'2026-09-20'}},'html_url':'https://github.com/o/r/commit/'+'a'*40}
    assert entries(canonical(obj),'https://api.github.com/repos/o/r/commits/HEAD','github')[0]['revision']=='a'*40
    with pytest.raises(AcquisitionError):entries(b'{}','https://api.github.com/x','github')

def test_watch_backup_preserves_state_events(workspace):
    setup(workspace);w=watch(workspace);wid=w.register('config/watch.json')['watch_id']
    with Store(workspace) as s:backup=s.snapshot()
    assert any(a['restore_path']==w.prefix(wid)+'/state.json' for a in backup['artifacts'])

def test_unapproved_new_source_or_private_raw_is_rejected(workspace):
    _,config=setup(workspace);config['targets'][0]['raw']='data/private_context/x';workspace.write('config/invalid.json',canonical(config))
    with pytest.raises(BoundaryError):watch(workspace).register('config/invalid.json')

def test_foreground_interrupt_stops_and_unbounded_run_rejected(workspace):
    setup(workspace);w=watch(workspace);wid=w.register('config/watch.json')['watch_id']
    with pytest.raises(BoundaryError):w.run(wid,cycles=1000)
    w.poll=lambda *a,**k:(_ for _ in ()).throw(KeyboardInterrupt())
    assert w.run(wid)['status']=='STOPPED' and w.state(wid)['watch_status']=='DORMANT'

def test_conditional_http_wire_304_and_cross_origin_redirect_strips_validators():
    class Reply:
        def __init__(self,status,headers):self.status=status;self.headers=headers
        def getheaders(self):return list(self.headers.items())
        def read(self,n):return b'ok'
    seen=[];responses=[Reply(304,{'ETag':'"b"'})]
    class Connection:
        def __init__(self,*args):pass
        def request(self,method,target,**kw):seen.append((method,target,kw['headers']))
        def getresponse(self):return responses.pop(0)
        def close(self):pass
    resolver=lambda *a,**k:[(None,None,None,None,('8.8.8.8',443))]
    h=PublicHTTP(resolver=resolver,connection_factory=Connection)
    assert h.conditional_get('https://example.org/a',etag='"a"').status==304
    assert seen[-1][2]['If-None-Match']=='"a"' and 'Cookie' not in seen[-1][2]
    responses.extend([Reply(302,{'Location':'https://other.org/a'}),Reply(200,{})])
    h.conditional_get('https://example.org/a',etag='"a"')
    assert 'If-None-Match' not in seen[-1][2]
    responses.append(Reply(304,{}))
    with pytest.raises(AcquisitionError):h.get('https://example.org/a')

def test_p3_has_no_process_network_or_production_mutation_path(workspace,monkeypatch):
    setup(workspace);w=watch(workspace,HTTP(response(feed([('old',TEXT),('new','Agent context critical data integrity failure in synthetic test.')]))));wid=w.register('config/watch.json')['watch_id'];eid=w.poll(wid,force=True)['outcomes'][0]['events'][0];rp=reviewed(workspace,w,wid,eid,level='P3')
    def forbidden(*a,**k):raise AssertionError('P3 attempted external control')
    monkeypatch.setattr(subprocess,'run',forbidden);monkeypatch.setattr(os,'kill',forbidden)
    monkeypatch.setattr(PublicHTTP,'get',forbidden);monkeypatch.setattr(PublicHTTP,'conditional_get',forbidden)
    baseline={str(p):p.read_bytes() for p in (workspace.root/'config').rglob('*') if p.is_file()}
    result=w.review(wid,eid,rp)
    assert result['impact']['signal']=='BLOCKING_REVIEW_REQUIRED'
    assert all(Path(p).read_bytes()==body for p,body in baseline.items())

def test_relevance_low_and_uncertain_cannot_raise_impact(workspace):
    setup(workspace);w=watch(workspace,HTTP(response(feed([('old',TEXT),('new','Agent context unrelated update.')]))));wid=w.register('config/watch.json')['watch_id'];eid=w.poll(wid,force=True)['outcomes'][0]['events'][0];rp=reviewed(workspace,w,wid,eid);v=json.loads(workspace.read(rp));v['relevance']='LOW';workspace.write(rp,canonical(v),replace=True)
    with pytest.raises(BoundaryError):w.review(wid,eid,rp)
    assert counts(workspace)=={}

def test_incremental_evidence_inherits_private_question_classification(workspace):
    setup(workspace);w=watch(workspace,HTTP(response(feed([('old',TEXT),('new','Agent context new relevant experiment.')]))));wid=w.register('config/watch.json')['watch_id'];eid=w.poll(wid,force=True)['outcomes'][0]['events'][0];rp=reviewed(workspace,w,wid,eid);result=w.review(wid,eid,rp)
    with Store(workspace) as store:
        evidence=store.get(result['evidence_id'])
        assert evidence.classification=='PRIVATE'
        assert json.loads(evidence.text)['classification']=='PRIVATE'
    assert w.load(w.prefix(wid)+'/events/'+eid+'.json')['classification']=='PUBLIC'

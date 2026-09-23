from __future__ import annotations
import hashlib
import json
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from agent_research_intelligence.connectors.public_http import PublicHTTP, AcquisitionError
from agent_research_intelligence.connectors.research_documents import HTMLDocument, parse_feed, public_link
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical, verify_policy
from agent_research_intelligence.governance.project_reader import read_project_file, PROJECT_STATE
from agent_research_intelligence.storage.database import Store
from agent_research_intelligence.storage.models import ResearchOutput


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def normalized(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def freeze_guard(level):
    if level not in ('P0','P1','P2','P3'):raise BoundaryError('Invalid impact level')
    return {'level':level,'signal':{'P0':'RECORD','P1':'VNEXT_REVIEW','P2':'GATE_REVIEW_REQUIRED',
            'P3':'BLOCKING_REVIEW_REQUIRED'}[level], 'authority':'INTERNAL_RESEARCH_ONLY',
            'control_capabilities':[], 'production_effects':[], 'auto_implementation':False}


def entries(body, url, kind):
    """Only source metadata / necessary text, never browser/model fallback."""
    if kind=='feed':
        values=parse_feed(body,url)
        # A valid non-feed XML must not be silently consumed as an empty feed.
        if not values and not re.search(br'<(?:\w+:)?(?:feed|rss)(?:\s|>)',body):
            raise AcquisitionError('FEED_ROOT_INVALID')
        result=[]
        for e in values:
            key=e.get('id') or e.get('guid') or (e['links'][0] if e['links'] else None)
            if not key:raise AcquisitionError('FEED_ENTRY_ID_MISSING')
            text='\n'.join(e.get(k,'') for k in ('title','summary','description'))
            parser=HTMLDocument(url);parser.feed(text)
            result.append({'key':key,'revision':digest(e),'text':'\n'.join(parser.lines),
                'locator':e['links'][0] if e['links'] else url+'#'+key,'metadata':e})
        if len({e['key'] for e in result})!=len(result):raise AcquisitionError('FEED_DUPLICATE_IDS')
        return result
    if kind=='github':
        try:
            e=json.loads(body);sha=e['sha'];text=e['commit']['message']
            if not re.fullmatch('[0-9a-f]{40}',sha) or not isinstance(text,str):raise ValueError()
        except (ValueError,KeyError,TypeError) as exc:raise AcquisitionError('GITHUB_METADATA_INVALID') from exc
        return [{'key':'HEAD','revision':sha,'text':text,'locator':e.get('html_url',url),
                 'metadata':{'sha':sha,'date':e['commit'].get('committer',{}).get('date'),'scope':'latest commit metadata; not a complete repository diff'}}]
    if kind=='web':
        try:text=body.decode('utf-8')
        except UnicodeError as exc:raise AcquisitionError('WEB_ENCODING_INVALID') from exc
        if '<html' in text.lower() or '<!doctype html' in text.lower():
            p=HTMLDocument(url);p.feed(text);text='\n'.join(p.lines)
        if not text.strip():raise AcquisitionError('EMPTY_DOCUMENT')
        return [{'key':url,'revision':digest(body),'text':text,'locator':url,'metadata':{}}]
    raise BoundaryError('Unsupported watch kind')


class Watch:
    def __init__(self, workspace, http=None, clock=None, hook=None, guard=None, evidence_mode="REAL"):
        if evidence_mode not in ("REAL","FIXTURE"):raise BoundaryError("Invalid evidence mode")
        self.evidence_mode=evidence_mode
        self.ws=workspace;self.http=http or PublicHTTP(timeout=15,max_bytes=2*1024*1024)
        self.clock=clock or (lambda:datetime.now(timezone.utc));self.hook=hook or (lambda phase:None)
        if guard is None:
            def guard():
                import psutil
                from agent_research_intelligence.acquisition.audio_runtime import Budget
                Budget(workspace,psutil).check()
        self.guard=guard

    def now(self):return self.clock().isoformat()
    def load(self,path):return json.loads(self.ws.read(path))
    def save(self,path,value):return self.ws.write(path,canonical(value))
    def prefix(self,wid):
        if not re.fullmatch('[0-9a-f]{32}',wid):raise BoundaryError('Invalid watch ID')
        return 'data/watch/'+wid
    def state(self,wid):return self.load(self.prefix(wid)+'/state.json')

    def commit(self,wid,state,reason):
        state={**state,'state_revision':state.get('state_revision',0)+1,'state_reason':reason}
        path=self.prefix(wid)+f"/states/{state['state_revision']:06d}-{digest(state)[:16]}.json"
        if not (self.ws.root/path).exists():self.save(path,state)
        self.hook('before_cursor_commit')
        self.ws.write(self.prefix(wid)+'/state.json',canonical(state),replace=True)
        return state

    def register(self,config_path):
        verify_policy(self.ws);self.guard();config=self.load(config_path)
        job=config['job'];review=config['dossier']
        if not re.fullmatch('[0-9a-f]{32}',job) or not review.startswith('data/discovery/'+job+'/reviews/') or not review.endswith('/dossier.md'):
            raise BoundaryError('Watch requires an existing discovery dossier and question')
        original=self.ws.read(review);sources=self.load('data/discovery/'+job+'/sources.json');by_id={s['id']:s for s in sources}
        question=self.ws.read('data/discovery/'+job+'/question.json');ev=self.load(review.rsplit('/',1)[0]+'/evidence.json')
        if not 1<=len(config['targets'])<=8:raise BoundaryError('Bounded targeted watch required')
        terms=config['local_terms']
        if not isinstance(terms,list) or not terms or any(not isinstance(t,str) or not 1<=len(t)<=100 for t in terms):raise BoundaryError('Local terms required')
        days=config.get('watch_days',90)
        if not isinstance(days,int) or not 1<=days<=90:raise BoundaryError('Watch window must be 1..90 days')
        with Store(self.ws):
            wid=uuid4().hex;p=self.prefix(wid);targets={};corpus=[]
            for e in ev:
                doc=self.load(e['document'])
                if digest(self.ws.read(e['document']))!=e['document_sha256']:raise BoundaryError('Prior evidence drift')
                a,b=e['lines'];corpus.append({'ref':e['id'],'text':normalized('\n'.join(doc['lines'][a-1:b]))})
            for t in config['targets']:
                sid=t['source_id']
                if sid not in by_id or sid in targets:raise BoundaryError('Unknown or repeated source')
                docpath=t['document']
                if not docpath.startswith('data/discovery/'+job+'/'):raise BoundaryError('Source outside dossier discovery')
                doc=self.load(docpath);url=by_id[sid]['url'];kind=t['kind']
                if doc.get('requested_url',doc['url'])!=url and doc['url']!=url:raise BoundaryError('Source identity mismatch')
                poll_url=url
                if kind=='github':
                    match=re.fullmatch(r'https://github.com/([\w.-]+)/([\w.-]+)/?',url)
                    if not match:raise BoundaryError('GitHub repository URL required')
                    poll_url=f'https://api.github.com/repos/{match[1]}/{match[2]}/commits/HEAD'
                    baseline=[{'key':'HEAD','revision':doc['version']}];raw_hash=None
                else:
                    rawpath=t['raw']
                    if not rawpath.startswith('data/discovery/'+job+'/'):raise BoundaryError('Raw source outside job')
                    body=self.ws.read(rawpath,limit=2*1024*1024)
                    if digest(body)!=doc['version']:raise BoundaryError('Raw source hash differs from sealed document')
                    baseline=entries(body,url,kind);raw_hash=digest(body)
                if public_link(poll_url,poll_url)!=poll_url:raise BoundaryError('Public source URL required')
                # No socket access here; actual polls enforce PublicHTTP DNS pinning.
                targets[sid]={'url':poll_url,'source_url':url,'kind':kind,'raw_hash':raw_hash,'etag':None,'modified':None,
                    'seen':{e['key']:e['revision'] for e in baseline},'next_poll_at':None,'last_change_at':None,
                    'interval_s':3600 if kind in ('feed','github') else 86400,'baseline':docpath,'baseline_sha256':digest(self.ws.read(docpath))}
                for e in baseline:
                    if e.get('text'):corpus.append({'ref':'BASELINE:'+sid+':'+e['key'],'text':normalized(e['text'])})
            state={'id':wid,'job':job,'question_sha256':digest(question),'original_dossier':review,'original_dossier_sha256':digest(original),
                'current_dossier':review,'rq_status':'WATCHING','watch_status':'WATCHING','created_at':self.now(),
                'watch_until':(self.clock()+timedelta(days=days)).isoformat(),'reopen_threshold':0.8,'local_terms':terms,
                'targets':targets,'corpus':corpus,'processed_events':[],'reviewed_events':{},'state_revision':0,
                'evidence_mode':self.evidence_mode,'scope':'READ_ANALYZE_PROPOSE_ONLY','classification':'PRIVATE','account_mutation':'recommend_only'}
            self.save(p+'/registration.json',{'config':config,'at':self.now(),'source':'EXPLICIT_LOCAL_REGISTRATION','dossier_sha256':digest(original)})
            self.commit(wid,state,'REGISTERED')
            return {'watch_id':wid,'state':'WATCHING','targets':len(targets),'watch_until':state['watch_until'],'network_requests':0,'model_calls':0}

    def control(self,wid,action):
        if action not in ('stop','resume'):raise BoundaryError('Unknown local watch action')
        with Store(self.ws):
            s=self.state(wid)
            if action=='resume' and self.clock()>=datetime.fromisoformat(s['watch_until']):raise BoundaryError('Expired watch cannot be silently extended')
            s['watch_status']='DORMANT' if action=='stop' else 'WATCHING';self.commit(wid,s,action.upper())
            return {'watch_id':wid,'state':s['watch_status'],'external_effects':[]}

    def poll(self,wid,*,force=False):
        verify_policy(self.ws);self.guard()
        with Store(self.ws):
            s=self.state(wid)
            if s.get('evidence_mode','REAL')!=self.evidence_mode:raise BoundaryError('Watch evidence mode mismatch')
            p=self.prefix(wid);attempt=uuid4().hex;ap=p+'/polls/'+attempt;started=time.monotonic();start_trace=len(self.http.trace);start_requests=getattr(self.http,"total_requests",start_trace)
            cost={'poll_requests':0,'response_bytes':0,'new_source_items':0,'deep_analysis_calls':0,'model_calls':0,'tokens':0,
                  'deterministic_change_checks':0,'deterministic_content_screens':0,'semantic_reviews':0,'new_evidence':0,'dossier_revisions':0}
            receipt={'at':self.now(),'watch_id':wid,'attempt':attempt,'classification':'REAL_PUBLIC_READ' if self.evidence_mode=='REAL' else 'TEST_FIXTURE','outcomes':[],'cost':cost}
            if s['watch_status']!='WATCHING':receipt['outcomes'].append({'status':'DORMANT'})
            elif self.clock()>=datetime.fromisoformat(s['watch_until']):
                s['watch_status']='DORMANT';self.commit(wid,s,'WATCH_EXPIRED');receipt['outcomes'].append({'status':'EXPIRED'})
            else:
                for sid in list(s['targets']):
                    t=s['targets'][sid]
                    if t.get('retry_at') and self.clock()<datetime.fromisoformat(t['retry_at']):
                        receipt['outcomes'].append({'source_id':sid,'status':'RETRY_NOT_DUE'});continue
                    if not force and t['next_poll_at'] and self.clock()<datetime.fromisoformat(t['next_poll_at']):
                        receipt['outcomes'].append({'source_id':sid,'status':'NOT_DUE'});continue
                    cost['poll_requests']+=1
                    try:
                        response=self.http.conditional_get(t['url'],etag=t['etag'],modified=t['modified'])
                        cost['response_bytes']+=len(response.body);cost['deterministic_change_checks']+=1
                        self.ws.write(ap+'/'+sid+'.raw',response.body)
                        rawhash=digest(response.body);unchanged=response.status==304 or rawhash==t['raw_hash']
                        if response.status==304 and not (t['etag'] or t['modified']):raise AcquisitionError('UNEXPECTED_304')
                        new_items=[];coverage='TARGET_WINDOW_ONLY'
                        if not unchanged:
                            current=entries(response.body,response.url,t['kind'])
                            if t['kind']=='feed' and t['seen'] and current and not set(t['seen']).intersection(e['key'] for e in current):coverage='POSSIBLE_FEED_WINDOW_GAP'
                            new_items=[e for e in current if t['seen'].get(e['key'])!=e['revision']]
                        outcome={'source_id':sid,'status':'NO_CHANGE' if unchanged or not new_items else 'CHANGES_RECORDED',
                            'coverage':coverage,'http_status':response.status,'response_sha256':rawhash,'source_revision':t['raw_hash'] if response.status==304 else rawhash,'events':[]}
                        # Validators become eligible only after parsing and artifact persistence.
                        for e in new_items:
                            eid=digest([wid,sid,e['key'],e['revision']]);ep=p+'/events/'+eid
                            if eid not in s['processed_events']:
                                text=normalized(e['text']);hits=[term for term in s['local_terms'] if term.casefold() in text]
                                matches=[c['ref'] for c in s['corpus'] if text and (text==c['text'] or (len(text.split())>=20 and text in c['text']))]
                                cost['deterministic_content_screens']+=1
                                ev={'event_id':eid,'source_id':sid,'source_url':t['source_url'],'source_revision':e['revision'],'source_item_key':e['key'],
                                    'locator':e['locator'],'text':e['text'],'metadata':e['metadata'],'question_job':s['job'],
                                    'relevance':{'status':'CANDIDATE' if hits or matches else 'NO_KEYWORD_MATCH_UNCERTAIN','basis':hits},
                                    'novelty':{'status':'EXACT_DUPLICATE' if matches else 'UNASSESSED','existing_evidence':matches},
                                    'impact':{'status':'UNASSESSED'},'disposition':'RELEVANT_DUPLICATE' if matches else 'PENDING_SEMANTIC_REVIEW',
                                    'classification':'PUBLIC','evidence_mode':self.evidence_mode,'authority':'EXTERNAL_EVIDENCE','prediction_of_truth':False}
                                if (self.ws.root/(ep+'.json')).exists():
                                    if self.load(ep+'.json')!=ev:raise BoundaryError('Event collision')
                                else:self.save(ep+'.json',ev)
                                self.hook('after_event_saved')
                                s['processed_events'].append(eid);cost['new_source_items']+=1
                            outcome['events'].append(eid);t['seen'][e['key']]=e['revision']
                        self.save(ap+'/'+sid+'.result.json',outcome)
                        if response.status!=304:t['raw_hash']=rawhash
                        # A 200 without validators clears old ones; 304 may refresh them.
                        for field,header in (('etag','etag'),('modified','last-modified')):
                            if response.status!=304 or header in response.headers:t[field]=response.headers.get(header)
                        if new_items:
                            if t['last_change_at']:
                                elapsed=(self.clock()-datetime.fromisoformat(t['last_change_at'])).total_seconds()
                                t['interval_s']=max(3600,min(7*86400,int(elapsed)))
                            t['last_change_at']=self.now()
                        else:t['interval_s']=min(7*86400,t['interval_s']*2)
                        t['retry_at']=None;t['failures']=0;t['next_poll_at']=(self.clock()+timedelta(seconds=t['interval_s'])).isoformat()
                        s=self.commit(wid,s,'POLL_ARTIFACTS_SAVED');receipt['outcomes'].append(outcome)
                    except (AcquisitionError,ValueError,OSError) as exc:
                        # Reload committed cursor; no in-memory partial updates survive failure.
                        s=self.state(wid);t=s['targets'][sid];t['failures']=t.get('failures',0)+1
                        delay=getattr(exc,'retry_after',None) or min(86400,60*2**min(t['failures'],10))
                        t['retry_at']=(self.clock()+timedelta(seconds=min(delay,7*86400))).isoformat()
                        error={'source_id':sid,'status':'FAILED_NOT_CONSUMED','reason':getattr(exc,'code',type(exc).__name__),'retry_at':t['retry_at']}
                        self.save(ap+'/'+sid+'.failure.json',error);self.commit(wid,s,'FAILURE_RETRY_ONLY');receipt['outcomes'].append(error)
            request_count=getattr(self.http,'total_requests',len(self.http.trace))-start_requests
            receipt['cost']['actual_http_requests_including_redirects']=request_count
            receipt['transport_trace']=self.http.trace[-request_count:] if request_count else [];receipt['elapsed_s']=time.monotonic()-started
            receipt['cost']['artifact_bytes_before_receipt']=sum(f.stat().st_size for f in (self.ws.root/ap).glob('*') if f.is_file()) if (self.ws.root/ap).exists() else 0
            self.save(ap+'/receipt.json',receipt);return {**receipt,'receipt':ap+'/receipt.json'}

    def review(self,wid,event_id,review_path):
        verify_policy(self.ws);self.guard();p=self.prefix(wid)
        if not re.fullmatch('[0-9a-f]{64}',event_id):raise BoundaryError('Invalid event ID')
        review=self.load(review_path);event=self.load(p+'/events/'+event_id+'.json')
        if review.get('event_sha256')!=digest(self.ws.read(p+'/events/'+event_id+'.json')):raise BoundaryError('Review event drift')
        if review.get('origin') not in ('CODEX_LOCAL_REVIEW','HUMAN_REVIEW','FIXTURE'):raise BoundaryError('Explicit semantic reviewer origin required')
        if not isinstance(review.get('reason'),str) or not review['reason'].strip():raise BoundaryError('Located reasoning required')
        relevance=review.get('relevance');novelty=review.get('novelty');level=review.get('impact')
        if relevance not in ('HIGH','LOW','UNKNOWN') or novelty not in ('HIGH','DUPLICATE','UNKNOWN'):raise BoundaryError('Separated relevance / novelty judgments required')
        signal=freeze_guard(level);score=review.get('impact_score')
        if not isinstance(score,(int,float)) or not 0<=score<=1:raise BoundaryError('Impact score required')
        if not review.get('affected') or not isinstance(review['affected'],list):raise BoundaryError('Impact target/rationale required')
        with Store(self.ws) as store:
            s=self.state(wid)
            if s.get('evidence_mode','REAL')=='FIXTURE' and review['origin']!='FIXTURE':raise BoundaryError('Fixture cannot be reviewed as real evidence')
            if s.get('evidence_mode','REAL')=='REAL' and review['origin']=='FIXTURE':raise BoundaryError('Fixture review cannot alter real watch')
            if event_id not in s['processed_events']:raise BoundaryError('Uncommitted source event cannot be reviewed')
            key=digest(review);rp=p+'/reviews/'+event_id+'/'+key
            if event_id in s['reviewed_events']:
                if s['reviewed_events'][event_id]!=key:raise BoundaryError('Already reviewed: preserve decision; no silent replacement')
                return self.load(rp+'/result.json')
            if novelty=='DUPLICATE':
                refs={c['ref'] for c in s['corpus']}
                if not review.get('duplicate_refs') or not set(review['duplicate_refs'])<=refs:raise BoundaryError('Duplicate must link existing evidence')
            a,b=review.get('lines',[0,0]);lines=event['text'].splitlines()
            if not isinstance(a,int) or not isinstance(b,int) or not 1<=a<=b<=len(lines):raise BoundaryError('Review needs exact new-source lines')
            novel=relevance=='HIGH' and novelty=='HIGH';reopen=novel and level in ('P2','P3') and score>=s['reopen_threshold']
            if not novel and level!='P0':raise BoundaryError('Unconfirmed or duplicate evidence cannot raise impact')
            if novel and not review.get('finding'):raise BoundaryError('New evidence needs an explicit finding')
            self.save(rp+'/review.json',review) if not (self.ws.root/(rp+'/review.json')).exists() else None
            result={'watch_id':wid,'event_id':event_id,'relevance':relevance,'novelty':novelty,'impact':signal,
                    'rq_state': 'REOPENED' if reopen else s['rq_status'],'new_evidence':0,'dossier_revision':None,
                    'semantic_review_origin':review['origin'],'cost':{'semantic_reviews':1,'deep_analysis_calls':0,'model_calls':0,'tokens':0,'external_review_token_usage':'NOT_MEASURED_BY_TOOL'},
                    'limits':'Local explicit semantic review is analysis; no LLM is called by this service. Source assertions are not verified facts.'}
            if novel:
                eid=digest([wid,event_id,'evidence']);ev={'finding':review['finding'],'source_revision':event['source_revision'],'source_url':event['source_url'],
                    'locator':event['locator'],'lines':[a,b],'excerpt':'\n'.join(lines[a-1:b]),'origin':review['origin'],'rq':s['job'],'trigger':event_id,
                    'limits':review['reason'],'classification':'PRIVATE','authority':'EXTERNAL_EVIDENCE'}
                if not (self.ws.root/(rp+'/evidence.json')).exists():self.save(rp+'/evidence.json',ev)
                store.append_once(eid,ResearchOutput(kind='evidence',classification='PRIVATE',text=json.dumps(ev,ensure_ascii=False),source_refs=(rp+'/evidence.json',)))
                result.update(new_evidence=1,evidence_id=eid);s['corpus'].append({'ref':eid,'text':normalized(ev['excerpt'])})
                if reopen:
                    parent=s['current_dossier'];body=self.ws.read(parent).decode()
                    add='\n\n## watch incremental revision — '+review['origin']+'\n\n'+review['finding']+'\n\nSource: '+event['locator']+' / revision '+event['source_revision']+' / lines '+str(a)+'–'+str(b)+'\n\nLimits: '+review['reason']+'\n\nInternal review signal: '+signal['signal']+'; no production authority.\n'
                    path=rp+'/dossier.md'
                    if not (self.ws.root/path).exists():self.ws.write(path,(body+add).encode())
                    lineage={'parent':parent,'parent_sha256':digest(self.ws.read(parent)),'question':s['job'],'trigger_event':event_id,'source_revision':event['source_revision'],'dossier':path}
                    if not (self.ws.root/(rp+'/lineage.json')).exists():self.save(rp+'/lineage.json',lineage)
                    store.append_once(digest([wid,event_id,'dossier']),ResearchOutput(kind='dossier',classification='PRIVATE',text=body+add,source_refs=(path,parent,rp+'/lineage.json')))
                    s['current_dossier']=path;s['rq_status']='REOPENED';result['dossier_revision']=path
            if not (self.ws.root/(rp+'/result.json')).exists():self.save(rp+'/result.json',result)
            self.hook('after_review_artifacts')
            s['reviewed_events'][event_id]=key;self.commit(wid,s,'REVIEW_PERSISTED')
            return result

    def run(self,wid,cycles=1,interval=60):
        if not 1<=cycles<=12 or not 30<=interval<=3600:raise BoundaryError('Foreground run must be finite: 1..12 cycles, 30..3600 seconds')
        results=[]
        try:
            for n in range(cycles):
                result=self.poll(wid);results.append(result['receipt'])
                if self.state(wid)['watch_status']=='DORMANT':break
                if n+1<cycles:time.sleep(interval)
        except KeyboardInterrupt:
            self.control(wid,'stop')
            return {'status':'STOPPED','receipts':results,'resume':'Explicit watch-resume; committed cursor retained'}
        return {'status':'BOUNDED_RUN_COMPLETE','receipts':results,'background_processes':0}

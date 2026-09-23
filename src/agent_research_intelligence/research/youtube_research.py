"""Normal CLI research: registry -> recent videos -> bounded L0/L1/L2/L3.

Queries alone may leave the machine, after an exact public-brief approval.
Private RQ/context and subscription inventories are never sent as search text.
"""
import hashlib
import json
import time
from uuid import uuid4

from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.connectors.public_http import PublicHTTP
from agent_research_intelligence.connectors.youtube_sources import YouTubeSources,channel_id
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical,ClassifiedText
from agent_research_intelligence.research.catalog import Catalog,now
from agent_research_intelligence.research.scoring import screen


class YouTubeResearch:
    def __init__(self,workspace,*,sources=None,guard=None,acquire=None,mode='REAL'):
        if mode not in ('REAL','FIXTURE') or mode=='REAL' and (sources is not None or acquire is not None):
            raise BoundaryError('Injected research providers must be explicitly FIXTURE')
        self.mode=mode
        self.ws=workspace;self.catalog=Catalog(workspace)
        from agent_research_intelligence.connectors.youtube_account import YouTubeAccount
        account=YouTubeAccount(workspace)
        self.account=account
        self.resource_probe=None
        if guard is None:
            guard=account._resource_guard
        self.guard=guard
        self.sources=sources or YouTubeSources(PublicHTTP(timeout=20, max_bytes=8 * 1024 * 1024),self.guard,account=account)
        self.acquire=acquire

    def _save(self,path,value):
        raw=canonical(value);self.ws.write(path,raw)
        return {'artifact':path,'sha256':hashlib.sha256(raw).hexdigest(),'locator':'whole document','classification':self.mode}

    def import_subscriptions(self,path):
        if not path.startswith('data/youtube/account/'):raise BoundaryError('Owned account receipt required')
        raw=self.ws.read(path);receipt=json.loads(raw)
        from agent_research_intelligence.connectors.youtube_account import SCOPE
        if receipt.get('kind')!='AUTHENTICATED_SUBSCRIPTION_READ' or receipt.get('scope')!=SCOPE or receipt.get('account_mutation') is not False:
            raise BoundaryError('Not a subscription-read receipt')
        existing={r['payload']['platform_id']:r for r in self.catalog.search('source') if r['payload']['kind']=='channel'}
        rows=[]
        for i,item in enumerate(receipt['items']):
            cid=channel_id(item['channel_id']);old=existing.get(cid)
            basis={'artifact':path,'sha256':hashlib.sha256(raw).hexdigest(),'locator':f'items[{i}]','classification':'REAL'}
            payload={**(old['payload'] if old else {}),'url':'https://www.youtube.com/channel/'+cid,
                'entity_name':item['title'],'platform':'youtube','kind':'channel','platform_id':cid,
                'source_provenance':'AUTHENTICATED_SUBSCRIPTION_READ','subscription_state':'SUBSCRIBED',
                'basis':(old['payload']['basis'] if old else [])+[basis]}
            row=self.catalog.put('source',payload,identifier=old['id'] if old else None,
                                 expected_revision=old['revision'] if old else 0,reason='Observed real read-only subscription')
            rows.append(row['id'])
        return {'sources':rows,'subscriptions_complete':receipt['complete'],'trust_changed':False,
                'limitations':'Absent channels are not marked unsubscribed from a partial or filtered read.'}

    def discover(self,brief_path,approval_path):
        brief=json.loads(self.ws.read(brief_path));approval=json.loads(self.ws.read(approval_path))
        if (brief.get('classification')!='PUBLIC' or approval.get('authority')!='USER_AUTHORIZED_PUBLIC_RESEARCH'
                or approval.get('brief_sha256')!=hashlib.sha256(canonical(brief)).hexdigest()
                or not approval.get('source_message')):raise BoundaryError('Approved public brief required')
        queries=brief.get('queries',[])
        if not 1<=len(queries)<=4:raise BoundaryError('One to four approved public queries required')
        self.guard();prefix='data/youtube/discovery/'+uuid4().hex;trace=[];new=[]
        known={r['payload']['platform_id']:r for r in self.catalog.search('source') if r['payload']['kind']=='channel'}
        for n,q in enumerate(queries):
            try:
                response,candidates=self.sources.search(ClassifiedText(text=q['text'],classification='PUBLIC'))
                raw_path=prefix+f'/search-{n}.raw';self.ws.write(raw_path,response.body)
                candidate_path=prefix+f'/search-{n}.json';b=self._save(candidate_path,{'query':q['text'],'candidates':candidates,
                    'raw':raw_path,'raw_sha256':hashlib.sha256(response.body).hexdigest()})
                trace.append({'query':q['text'],'status':'TOOL_SEARCH','artifact':candidate_path,'candidates':len(candidates)})
                for candidate in candidates:
                    cid=candidate['channel_id']
                    if cid in known:continue
                    record=self.catalog.put('source',dict(url='https://www.youtube.com/channel/'+cid,
                        entity_name=candidate['channel'],platform='youtube',kind='channel',platform_id=cid,
                        source_provenance='TOOL_DISCOVERY',basis=[b],subscription_state='RECOMMEND_ONLY',
                        limitations=['Discovered in public search. Neither user subscription nor established expertise.']),reason='YouTube open-world search discovery')
                    known[cid]=record;new.append(record['id'])
            except (AcquisitionError,BoundaryError,ValueError,KeyError) as exc:
                trace.append({'query':q.get('text'),'status':'FAILED','reason':getattr(exc,'code',type(exc).__name__)})
        result={'sources_added':new,'trace':trace,'account_mutations':0,'subscription_recommendations':[],
                'sample_before_recommendation':new,
                'meaning':'Candidate sources; inspect 3–5 videos before treating density as representative.'}
        self._save(prefix+'/result.json',result)
        return {**result,'artifact':prefix+'/result.json'}

    def profile(self,source_id,rq_id,*,samples=3):
        if not 3<=samples<=5:raise BoundaryError('Creator sampling uses three to five candidates')
        from agent_research_intelligence.acquisition.youtube import YouTube
        from agent_research_intelligence.research.scoring import creator_density
        source=self.catalog.read(source_id);question=self.catalog.read(rq_id)
        if source['kind']!='source' or source['payload']['kind']!='channel' or question['kind']!='question':
            raise BoundaryError('Channel source and question required')
        if self.mode=='REAL' and any(b['classification']=='FIXTURE' for b in source['payload']['basis']):
            raise BoundaryError('Fixture source cannot become a REAL profile')
        self.guard();prefix='data/youtube/profiles/'+uuid4().hex
        response,recent=self.sources.recent(source['payload']['platform_id'])
        self.ws.write(prefix+'/feed.xml',response.body)
        # Fixed newest feed order, not chosen after transcript quality is observed.
        selected=recent[:samples];out=[];failures=[]
        for item in selected:
            try:
                _,meta=self.sources.metadata(item['url']);v=YouTube(self.sources.http).fetch(item['url'],languages=('en','zh'))
                content=v.model_dump(mode='json');self._save(prefix+'/'+item['video_id']+'.transcript.json',content)
                out.append(screen(meta,question['payload']['search_terms'],segments=content['segments']))
            except (AcquisitionError,BoundaryError,ValueError,KeyError) as e:
                failures.append({'video_id':item['video_id'],'reason':getattr(e,'code',type(e).__name__)})
        assessment=creator_density(out) if out else {'status':'INSUFFICIENT_EVIDENCE','sample_count':0,'aggregate':None}
        result={'source_id':source_id,'rq_id':rq_id,'selected_video_ids':[e['video_id'] for e in selected],
            'assessment':assessment,'failures':failures,'classification':self.mode,
            'recommendation':'RECOMMEND_SUBSCRIBE_FOR_USER_REVIEW' if len(out)>=3 and any(s['dimensions']['task_relevance']['value'] for s in out)
                             else 'INSUFFICIENT_EVIDENCE','account_mutations':0,
            'limits':'Lexical density is only a budget signal; no general expertise/trust or factual correctness established.'}
        b=self._save(prefix+'/result.json',result)
        updated=self.catalog.put('source',{**source['payload'],'density_assessment':assessment,
            'status':'CANDIDATE' if source['payload']['status']=='DISCOVERED' else source['payload']['status'],
            'basis':source['payload']['basis']+[b]},identifier=source_id,expected_revision=source['revision'],reason='Bounded creator samples, no trust promotion')
        return {**result,'source_revision':updated['revision'],'artifact':prefix+'/result.json'}

    def workflow(self,rq_id,*,brief_path=None,approval_path=None,**budgets):
        """Registry first; one bounded discovery pass only when coverage is thin."""
        rq=self.catalog.read(rq_id)
        if rq['kind']!='question':raise BoundaryError('Question required')
        known=[s for s in self.catalog.search('source') if s['payload']['kind']=='channel'
               and s['payload']['status']!='ARCHIVED' and s['payload']['source_provenance']!='DEVELOPMENT_SEED'
               and (self.mode=='FIXTURE' or not any(b['classification']=='FIXTURE' for b in s['payload']['basis']))]
        matched=[s for s in known if any(t.casefold() in (s['payload']['entity_name']+' '+' '.join(s['payload']['topics'])).casefold()
                                        for t in rq['payload']['search_terms'])]
        coverage={'known_channels':len(known),'metadata_topic_matches':len(matched),
                  'needs_discovery':len(known)<10 or not matched,'meaning':'Coverage screen, not a quality judgment'}
        discovered=None
        if coverage['needs_discovery'] and brief_path and approval_path:
            discovered=self.discover(brief_path,approval_path)
        elif bool(brief_path)!=bool(approval_path):raise BoundaryError('Public brief and approval must be supplied together')
        result=self.run(rq_id,**budgets)
        path='data/youtube/workflows/'+uuid4().hex+'.json'
        record={'rq_id':rq_id,'coverage_before':coverage,'discovery':discovered,'research_result':result['artifact'],
                'discovery_not_run_reason':'COVERAGE_SUFFICIENT' if not coverage['needs_discovery'] else 'PUBLIC_BRIEF_REQUIRED' if not discovered else None,
                'classification':self.mode,'account_mutations':0}
        self._save(path,record)
        return {**result,'workflow_artifact':path,'coverage_before':coverage,'discovery':discovered}

    def run(self,rq_id,*,max_sources=20,max_metadata=30,max_light=5,max_deep=2,asr_profile=None,
            material_type='UNKNOWN',diarization_provider=None):
        if not (1<=max_sources<=50 and 1<=max_metadata<=50 and 1<=max_light<=5 and 1<=max_deep<=3):
            raise BoundaryError('Research budgets exceeded')
        if self.mode=='REAL':
            from agent_research_intelligence.research.resource_probe import ResourceProbe
            self.resource_probe=ResourceProbe.claim_next(self.ws)
            if self.resource_probe:
                self.account.resource_probe=self.resource_probe
                if self.account._budget is not None:
                    self.account._budget.on_memory_stop=self.resource_probe.stop_snapshot
                self.resource_probe.set_stage('SOURCE_SELECTION')
        started=time.monotonic();started_at=now()
        initial_requests=getattr(getattr(self.sources,'http',None),'total_requests',0)
        initial_api=len(getattr(getattr(self.sources,'account',None),'trace',[]))
        rq=self.catalog.read(rq_id)
        if rq['kind']!='question':raise BoundaryError('Research question required')
        terms=rq['payload']['search_terms'];self.guard()
        source_rows=[r for r in self.catalog.search('source') if r['payload']['kind']=='channel'
                     and r['payload']['status']!='ARCHIVED' and r['payload']['source_provenance']!='DEVELOPMENT_SEED'
                     and (self.mode=='FIXTURE' or not any(b['classification']=='FIXTURE' for b in r['payload']['basis']))]
        source_rows=sorted(source_rows,key=lambda r:(-sum(t.casefold() in json.dumps(r['payload'],ensure_ascii=False).casefold() for t in terms),r['id']))[:max_sources]
        job=uuid4().hex;prefix='data/youtube/research/'+job;trace=[];candidates=[];states=[]
        if self.resource_probe:self.resource_probe.set_stage('L0_RECENT',job)
        if material_type not in ('UNKNOWN','ENGLISH_TECHNICAL_LECTURE') or diarization_provider not in (None,'community1'):
            raise BoundaryError('Invalid explicit material/diarization selection')
        config={'terms':terms,'max_sources':max_sources,'max_metadata':max_metadata,'max_light':max_light,'max_deep':max_deep,
                'asr_profile':asr_profile,'material_type':material_type,'diarization_provider':diarization_provider,
                'recent_policy':'API_UPLOADS_30_OR_PUBLIC_RSS_v1'}
        config_hash=hashlib.sha256(canonical(config)).hexdigest()
        self._save(prefix+'/request.json',{'rq_id':rq_id,'question_revision':rq['revision'],'config':config,
            'known_sources':[{'id':r['id'],'revision':r['revision'],'provenance':r['payload']['source_provenance']} for r in source_rows]})
        self._save(prefix+'/code-at-start.json',{str(p.relative_to(self.ws.root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.ws.root/'src/agent_research_intelligence').rglob('*.py') if not p.is_symlink()})
        for s in source_rows:
            self.guard();cid=channel_id(s['payload']['platform_id']);state_path=f'data/youtube/cursors/{rq_id}/{s["id"]}-{config_hash}.json'
            try:state=json.loads(self.ws.read(state_path))
            except (OSError,BoundaryError):state={}
            try:
                response,entries=self.sources.recent(cid,etag=state.get('etag'),modified=state.get('modified'))
                if response.status==304 and not (state.get('etag') or state.get('modified')):
                    raise AcquisitionError('UNEXPECTED_304')
                unchanged=response.status==304 or hashlib.sha256(response.body).hexdigest()==state.get('content_hash')
                feed_path=prefix+'/'+s['id']+'.feed';self.ws.write(feed_path,response.body)
                fresh=[e for e in entries if e['video_id'] not in state.get('seen',[])] if not unchanged else []
                trace.append({'source_id':s['id'],'stage':'L0','status':'NO_CHANGE' if unchanged else 'CHANGED',
                              'new_ids':len(fresh),'response_status':response.status,'artifact':feed_path,
                              'provider':response.headers.get('x-recent-provider','RSS'),
                              'fallback_reason':response.headers.get('x-fallback-reason') or None})
                for e in fresh:candidates.append({**e,'source_id':s['id'],'source_revision':s['revision']})
                states.append((state_path,{**state,'provider':response.headers.get('x-recent-provider','RSS'),
                    'origin':response.url,'fallback_reason':response.headers.get('x-fallback-reason') or None,
                    'etag':response.headers.get('etag',state.get('etag') if response.status==304 else None),
                    'modified':response.headers.get('last-modified',state.get('modified') if response.status==304 else None),
                    'content_hash':state.get('content_hash') if response.status==304 else hashlib.sha256(response.body).hexdigest(),
                    'seen':list(dict.fromkeys(state.get('seen',[])+[e['video_id'] for e in fresh])),'job':job}))
            except (AcquisitionError,BoundaryError,ValueError,KeyError) as exc:
                trace.append({'source_id':s['id'],'stage':'L0','status':'FAILED','reason':getattr(exc,'code',type(exc).__name__)})
        # Sort by relevance before spending metadata/transcript budget. Not by popularity.
        ranked=sorted(candidates,key=lambda e:(-sum(t.casefold() in e['title'].casefold() for t in terms),e['video_id']))
        selected=[];failed_sources=set();evidence=[]
        if self.resource_probe:self.resource_probe.set_stage('L1_METADATA')
        for e in ranked[:max_metadata]:
            try:
                response,metadata=self.sources.metadata(e['url'])
                if metadata['channel_id']!=next(s['payload']['platform_id'] for s in source_rows if s['id']==e['source_id']):raise BoundaryError('Candidate channel identity mismatch')
                self.ws.write(prefix+'/'+e['video_id']+'.metadata.raw',response.body)
                score=screen(metadata,terms)
                row={**e,'metadata':metadata,'screening':score}
                self._save(prefix+'/'+e['video_id']+'.metadata.json',row)
                trace.append({'video_id':e['video_id'],'stage':'L1','status':'RELEVANT_CANDIDATE' if score['dimensions']['task_relevance']['value'] else 'IRRELEVANT'})
                if score['dimensions']['task_relevance']['value']:selected.append(row)
            except (AcquisitionError,BoundaryError,ValueError,KeyError) as exc:
                failed_sources.add(e['source_id']);trace.append({'video_id':e['video_id'],'stage':'L1','status':'FAILED','reason':getattr(exc,'code',type(exc).__name__)})
        selected.sort(key=lambda r:(-r['screening']['budget_score_lower_bound'],r['video_id']))
        if self.acquire is None:
            from agent_research_intelligence.research.youtube_content import acquire_content
            acquire=lambda row,deep:acquire_content(self.ws,row,self.sources,self.guard,terms=terms,deep=deep,asr_profile=asr_profile)
        else:acquire=self.acquire
        light_results=[];light_candidates=selected[:max_light]
        if self.resource_probe:self.resource_probe.set_stage('L2_LIGHT_CONTENT')
        for row in light_candidates:
            try:
                output=acquire(row,False)
                row['light_artifact']=output['artifact']
                row['light_sha256']=hashlib.sha256(self.ws.read(output['artifact'])).hexdigest()
                row['light_screening']=screen(row['metadata'],terms,segments=output['segments'])
                self._save(prefix+'/'+row['video_id']+'.light.json',output)
                light_results.append({'video_id':row['video_id'],**output})
                trace.append({'video_id':row['video_id'],'stage':'L2','status':'LIGHT_CONTENT_READY'})
            except (AcquisitionError,BoundaryError,ValueError,KeyError) as exc:
                row['light_failure']=getattr(exc,'code',type(exc).__name__)
                trace.append({'video_id':row['video_id'],'stage':'L2','status':'UNAVAILABLE','reason':row['light_failure']})
        # L2 content informs the bounded L3 allocation. Missing captions remain
        # explicit and can reach the separately authorized audio fallback.
        light_candidates.sort(key=lambda r:(-r.get('light_screening',r['screening'])['budget_score_lower_bound'],r['video_id']))
        deep_candidates=light_candidates[:max_deep]
        for row in light_candidates[max_deep:]:
            if row.get('light_failure'):failed_sources.add(row['source_id'])
        selection_path=prefix+'/selection.json'
        selection_basis=self._save(selection_path,{'scope':'VERSIONED_V0_1_PUBLIC_RESEARCH','rq_id':rq_id,
            'video_ids':[r['video_id'] for r in deep_candidates],'configuration':config,
            'selection_basis':'L1 plus available L2 content; no quality-score cherry-picking',
            'light':[{'video_id':r['video_id'],'artifact':r.get('light_artifact'),'failure':r.get('light_failure')} for r in light_candidates]})
        for row in deep_candidates:
            row.update(selection_artifact=selection_path,selection_sha256=selection_basis['sha256'],
                       material_type=material_type,diarization_provider=diarization_provider,
                       identity_roster=next(s['payload'].get('identity_candidates',[]) for s in source_rows if s['id']==row['source_id']))
        results=[];dossier_source_ids=[s['id'] for s in source_rows]
        if self.resource_probe:self.resource_probe.set_stage('L3_DEEP_CONTENT')
        for index,row in enumerate(deep_candidates):
            try:
                output=acquire(row,True)
                for component in ('frames','caption_speaker_association'):
                    if (output.get(component) or {}).get('status')=='BLOCKED':
                        failed_sources.add(row['source_id'])
                        trace.append({'video_id':row['video_id'],'stage':'L3','status':'COMPONENT_BLOCKED','component':component})
                self._save(prefix+'/'+row['video_id']+'.content.json',output)
                results.append({'video_id':row['video_id'],**output})
                if index<max_deep:
                    bpath=output['artifact'];raw=self.ws.read(bpath)
                    basis={'artifact':bpath,'sha256':hashlib.sha256(raw).hexdigest(),'locator':'segments','classification':self.mode}
                    for span in output['segments']:
                        if not any(t.casefold() in span['text'].casefold() for t in terms):continue
                        item=self.catalog.put('evidence',dict(rq_id=rq_id,source_id=row['source_id'],source_revision=row['source_revision'],
                            published_at=row['metadata']['published_at'],captured_at=now(),claim=span['text'],
                            locator={'video_id':row['video_id'],'url':row['url'],'start_s':span['start'],'duration_s':span.get('duration'),
                                     'transcript_artifact':bpath},evidence_type='TRANSCRIPT',transcript_source=output['transcript_source'],
                            source_quality=row['screening'],relevance={'matched_terms':[t for t in terms if t.casefold() in span['text'].casefold()]},
                            novelty={'status':'UNASSESSED'},impact={'status':'UNASSESSED'},confidence='LOW',
                            limitations=['Source assertion from unverified transcript; not accepted truth.'],provenance=[basis],classification=self.mode),reason='Selected video transcript evidence')
                        evidence.append(item['id'])
                        if sum(self.catalog.read(i)['payload']['locator']['video_id']==row['video_id'] for i in evidence)>=4:break
                    extra=self._linked_evidence(rq_id,row,output,basis)
                    evidence.extend(extra['evidence']);dossier_source_ids.extend(extra['sources'])
            except (AcquisitionError,BoundaryError,ValueError,KeyError) as exc:
                failed_sources.add(row['source_id']);trace.append({'video_id':row['video_id'],'stage':'L2/L3','status':'FAILED','reason':getattr(exc,'code',type(exc).__name__)})
        dossier=None
        if self.resource_probe:self.resource_probe.set_stage('DOSSIER')
        if evidence:
            from agent_research_intelligence.research.dossier import build
            dossier=build(self.ws,rq_id,evidence_ids=evidence,source_ids=list(dict.fromkeys(dossier_source_ids)))
        result={'job_id':job,'rq_id':rq_id,'classification':self.mode,'known_source_count':len(source_rows),'meets_10_source_demonstration':len(source_rows)>=10,
            'coverage':'INSUFFICIENT' if len(source_rows)<10 or not evidence else 'LOCATED_EVIDENCE_REQUIRES_REVIEW',
            'discovery_recommended':len(source_rows)<10 or not selected,'recent_candidates':len(candidates),
            'metadata_attempts':min(len(ranked),max_metadata),'selected_video_ids':[r['video_id'] for r in deep_candidates],
            'light_content':light_results,'content':results,'evidence_ids':evidence,'dossier':dossier,'trace':trace,'deep_analysis_calls':0,'model_tokens':0,
            'limits':'L3 deep acquisition is not LLM analysis. ASR inference, if any, is accounted in its own receipt.',
            'state':'EVIDENCE_READY' if evidence else 'NO_NEW_EVIDENCE'}
        result.update(started_at=started_at,finished_at=now(),elapsed_s=time.monotonic()-started,
            configuration_sha256=config_hash,asr_model_runs=sum(o.get('model_runs',0) for o in results),
            source_http_requests=getattr(getattr(self.sources,'http',None),'total_requests',0)-initial_requests,
            api_requests=getattr(getattr(self.sources,'account',None),'trace',[])[initial_api:],
            resource_detail='Media/ASR/frame supervisors retain their own resource receipts; parent RSS peak not measured.',
            trace_limitations='Source transport count excludes separately reported external-resource/media transports.')
        from agent_research_intelligence.research.analysis_routing import plan
        result['analysis_route']=plan('cross_source_synthesis',changed=bool(candidates),relevant=bool(evidence),novel=bool(evidence),classification='PRIVATE')
        result['analysis_route']['novelty_limit']='New extracted claims require semantic novelty review; this budget plan establishes no novelty verdict.'
        artifact=prefix+'/result.json';self._save(artifact,result)
        # Commit only after artifacts. Failed sources retry all IDs (no lost failed event).
        # Budget-deferred IDs likewise must not be silently consumed.
        deferred={e['source_id'] for e in ranked[max_metadata:]}|{e['source_id'] for e in selected[max_light:]}
        for state_path,state in states:
            sid=state_path.rsplit('/',1)[-1].split('-')[0]
            if sid not in failed_sources|deferred:
                self.ws.write(state_path,canonical({**state,'artifact':artifact}),replace=True)
        if self.resource_probe:
            self.resource_probe.set_stage('COMPLETE')
            self.resource_probe.close()
        return {**result,'artifact':artifact}

    def _linked_evidence(self,rq_id,row,output,video_basis):
        """Every fetched linked resource and retained frame has a unified EvidenceItem."""
        evidence=[];sources=[]
        common=dict(rq_id=rq_id,published_at=None,captured_at=now(),source_quality={'status':'UNASSESSED'},
            relevance={'status':'DESCRIPTION_LINK_CANDIDATE'},novelty={'status':'UNASSESSED'},impact={'status':'UNASSESSED'},
            confidence='UNKNOWN',classification=self.mode)
        for resource in (output.get('external_resources') or {}).get('resources',[]):
            if resource['state']!='FETCHED':continue
            doc=json.loads(self.ws.read(resource['artifact']));b={
                'artifact':resource['artifact'],'sha256':hashlib.sha256(self.ws.read(resource['artifact'])).hexdigest(),
                'locator':'document lines','classification':self.mode}
            description=resource['description_artifact'];db={'artifact':description,
                'sha256':hashlib.sha256(self.ws.read(description)).hexdigest(),
                'locator':'description offset '+str(resource['description_offset']),'classification':self.mode}
            kind='github' if resource['resource_kind'] in ('github','skill') and 'github.com/' in resource['url'] else 'paper' if resource['resource_kind']=='paper' else 'web'
            src=self.catalog.put('source',dict(url=resource['url'],entity_name=doc['title'],platform=kind,kind=kind,
                source_provenance='TOOL_DISCOVERY',basis=[video_basis,db,b],limitations=['Description-linked source; separate retrieval does not establish independent corroboration.']),reason='Follow selected video description')
            sources.append(src['id'])
            text=next((line for line in doc['lines'] if line.strip()),'No extractable text; fetched resource only.')
            e=self.catalog.put('evidence',{**common,'source_id':src['id'],'source_revision':src['revision'],
                'claim':text,'locator':{'url':doc['locator'],'version':doc['version'],'artifact':resource['artifact'],
                                      'line':next((i+1 for i,l in enumerate(doc['lines']) if l.strip()),None),
                                      'video_id':row['video_id'],'provenance_chain':resource['provenance']},
                'evidence_type':'CODE' if kind=='github' else 'PAPER' if kind=='paper' else 'SOURCE_ASSERTION',
                'limitations':['Located source excerpt, not a verified technical finding; source may share the video publisher.'],
                'provenance':[video_basis,db,b]},reason='Description resource independently retrieved')
            evidence.append(e['id'])
        for frame in (output.get('frames') or {}).get('frames',[]):
            if not frame.get('artifact'):continue
            b={'artifact':frame['artifact'],'sha256':frame['sha256'],'locator':'entire frame','classification':self.mode}
            e=self.catalog.put('evidence',{**common,'source_id':row['source_id'],'source_revision':row['source_revision'],
                'claim':'Frame candidate at a transcript visual cue: '+frame['cue'],
                'locator':{'video_id':row['video_id'],'frame':frame['artifact'],'original_pts_s':frame['original_pts_s'],
                           'requested_original_s':frame['requested_original_s']},'evidence_type':'FRAME',
                'limitations':['Image pixels were acquired; visual code/architecture/benchmark content has not been established by inspection.'],
                'provenance':[video_basis,b]},reason='Traceable frame candidate')
            evidence.append(e['id'])
        return {'evidence':evidence,'sources':sources}

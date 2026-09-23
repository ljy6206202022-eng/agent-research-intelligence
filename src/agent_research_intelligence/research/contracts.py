"""Appendix B callable facade. Existing implementations retain their guards.

JSON operations are exposed by `project-rd contract`. AudioExtract is an internal
pipeline contract: its caller owns the media lease and finally-cleanup; it is
not exposed as an unleased download command.
"""
import hashlib
import json
from uuid import uuid4
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.research.catalog import Catalog,Basis


class ResearchContracts:
    def __init__(self,workspace):self.ws=workspace;self.catalog=Catalog(workspace)

    def ResearchQuestionCreate(self,payload):return self.catalog.put('question',payload,reason='Explicit research question')
    def ResearchQuestionRead(self,identifier,revision=None):return self._read_kind(identifier,'question',revision)
    def SourceSearch(self,terms=()):return {'sources':self.catalog.search('source',terms)}
    def SourceProfileRead(self,identifier,revision=None):return self._read_kind(identifier,'source',revision)
    def SourceProfileUpdate(self,payload,reason,identifier=None,expected_revision=0):
        return self.catalog.put('source',payload,reason=reason,identifier=identifier,expected_revision=expected_revision)
    def _read_kind(self,identifier,kind,revision=None):
        value=self.catalog.read(identifier,revision)
        if value['kind']!=kind:raise BoundaryError('Contract record kind mismatch')
        return value
    def CandidateDiscover(self,provider,request):
        if provider=='youtube':
            from agent_research_intelligence.research.youtube_research import YouTubeResearch
            return YouTubeResearch(self.ws).discover(**request)
        if provider=='multi_source':
            from agent_research_intelligence.research.discovery import DiscoveryResearch
            from agent_research_intelligence.research.legacy_bridge import project_discovery
            result=DiscoveryResearch(self.ws).run(**request)
            return {**result,'unified_contracts':project_discovery(self.ws,result['job'],classification='REAL')}
        raise BoundaryError('Unsupported discovery provider')
    def _guard(self):
        import psutil
        from agent_research_intelligence.acquisition.audio_runtime import Budget
        Budget(self.ws,psutil).check()
    def ContentFetch(self,url,kind=None):
        from agent_research_intelligence.connectors.research_documents import Documents
        from agent_research_intelligence.connectors.browser import Browser
        doc=Documents(browser=Browser(self.ws),guard=self._guard).fetch(url,kind=kind);raw=doc.pop('raw')
        prefix='data/artifacts/documents/'+uuid4().hex
        self.ws.write(prefix+'.raw',raw);doc['raw_sha256']=hashlib.sha256(raw).hexdigest()
        self.ws.write(prefix+'.json',canonical(doc))
        return {'artifact':prefix+'.json','raw':prefix+'.raw','sha256':doc['raw_sha256'],'document':doc}
    def TranscriptExtract(self,row,terms,deep=False,asr_profile=None):
        from agent_research_intelligence.research.youtube_content import acquire_content
        from agent_research_intelligence.research.youtube_research import YouTubeResearch
        service=YouTubeResearch(self.ws)
        return acquire_content(self.ws,row,service.sources,self._guard,terms=terms,deep=deep,asr_profile=asr_profile)
    def AudioExtract(self,job_id,budget):
        from agent_research_intelligence.acquisition.research_audio import validate_selection
        from agent_research_intelligence.acquisition.audio_runtime import run_worker
        job=json.loads(self.ws.read('data/audio/'+job_id+'/job.json'));validate_selection(self.ws,job)
        run_worker(self.ws,job_id,'media',budget,timeout_s=1200)
        return json.loads(self.ws.read('tmp/media/'+job_id+'/media.json'))
    def AudioTranscribe(self,source,diarization_provider=None):
        from agent_research_intelligence.acquisition.parakeet_audio import run_parakeet_audio
        return run_parakeet_audio(self.ws,source=source,diarization_provider=diarization_provider)
    def SpeakerDiarize(self,job_id):
        import psutil
        from agent_research_intelligence.acquisition.audio_runtime import run_worker,Budget,heavy_slot
        job=json.loads(self.ws.read('data/audio/'+job_id+'/job.json'))
        if job.get('diarization_provider')!='community1':raise BoundaryError('Explicit installed Community-1 selection required')
        with heavy_slot(self.ws):run_worker(self.ws,job_id,'diarize',Budget(self.ws,psutil))
        return json.loads(self.ws.read('tmp/media/'+job_id+'/diarize.json'))
    def SpeakerIdentityResolve(self,turns,segments,roster,visual_bindings=()):
        from agent_research_intelligence.acquisition.identity_resolution import resolve_speakers
        for item in roster:self.catalog.validate_basis([Basis.model_validate(b) for b in item['basis']])
        for item in visual_bindings:self.catalog.validate_basis([Basis.model_validate(item['frame_basis'])])
        return resolve_speakers(turns,segments,roster,visual_bindings=visual_bindings)
    def DescriptionLinkExtract(self,description,segments):
        from agent_research_intelligence.acquisition.youtube import description_links,Segment
        return description_links(description,tuple(Segment.model_validate(s) for s in segments))
    def ExternalArtifactResolve(self,metadata,segments,max_resources=6):
        from agent_research_intelligence.research.external_artifacts import resolve_description
        from agent_research_intelligence.connectors.research_documents import Documents
        from agent_research_intelligence.research.youtube_research import YouTubeResearch
        service=YouTubeResearch(self.ws)
        return resolve_description(self.ws,metadata,segments,Documents(guard=self._guard),max_resources=max_resources,
            recheck=lambda:service.sources.metadata('https://www.youtube.com/watch?v='+metadata['video_id']))
    def FrameExtract(self,row,segments):
        from agent_research_intelligence.acquisition.frames import extract_video_frames
        return extract_video_frames(self.ws,row,segments)
    def EvidenceCreate(self,payload):return self.catalog.put('evidence',payload,reason='Located external evidence')
    def EvidenceRead(self,identifier,revision=None):return self._read_kind(identifier,'evidence',revision)
    def EvidenceScore(self,metadata,terms,segments=None,verified_resources=None,observations=None):
        from agent_research_intelligence.research.scoring import screen
        for obs in (observations or {}).values():
            self.catalog.validate_basis([Basis.model_validate(b) for b in obs.get('basis',[])])
        return screen(metadata,terms,segments=segments,verified_resources=verified_resources,observations=observations)
    def NoveltyDetect(self,claim,rq_id):
        from agent_research_intelligence.watch.service import normalized
        matches=[e['id'] for e in self.catalog.search('evidence') if e['payload']['rq_id']==rq_id and normalized(e['payload']['claim'])==normalized(claim)]
        return {'novelty':'EXACT_DUPLICATE' if matches else 'UNKNOWN','existing_evidence':matches,
                'limits':'No exact match is not evidence of semantic novelty; requires located review.'}
    def ImpactAssess(self,level,reason,affected,evidence_ids):
        from agent_research_intelligence.watch.service import freeze_guard
        if not reason or not affected or not evidence_ids:raise BoundaryError('Located impact rationale required')
        for i in evidence_ids:self.EvidenceRead(i)
        return {**freeze_guard(level),'reason':reason,'affected':affected,'evidence_ids':evidence_ids,'authority':'INTERNAL_RESEARCH_ONLY'}
    def ArchitectureMap(self,evidence_id,capabilities):
        from agent_research_intelligence.research.dossier import architecture_map
        for capability in capabilities:self.catalog.validate_basis([Basis.model_validate(b) for b in capability['basis']])
        return architecture_map(self.EvidenceRead(evidence_id)['payload'],capabilities)
    def DossierBuild(self,**request):
        from agent_research_intelligence.research.dossier import build
        return build(self.ws,**request)
    def WatchRegister(self,config_path=None,catalog_request=None):
        from agent_research_intelligence.watch.service import Watch
        watch=Watch(self.ws)
        if bool(config_path)==bool(catalog_request):raise BoundaryError('Exactly one watch registration mode required')
        if config_path:return watch.register(config_path)
        from agent_research_intelligence.watch.catalog_bridge import register
        return register(watch,**catalog_request)
    def WatchPoll(self,watch_id,force=False):
        from agent_research_intelligence.watch.service import Watch
        return Watch(self.ws).poll(watch_id,force=force)
    def HealthSignalRecord(self,manifest_path,mode='REAL'):
        from agent_research_intelligence.health.service import Health
        return Health(self.ws,mode=mode).import_signals(manifest_path)
    def HealthTrendEvaluate(self,baseline_id,mode='REAL'):
        from agent_research_intelligence.health.service import Health
        return Health(self.ws,mode=mode).assess(baseline_id)
    def RootCauseCaseCreate(self,path,mode='REAL'):
        from agent_research_intelligence.health.service import Health
        return Health(self.ws,mode=mode).diagnose(path)
    def ExperimentRun(self,request):
        from agent_research_intelligence.research.experiments import Experiments
        return Experiments(self.ws).run(request)
    def ShadowRun(self,request):
        from agent_research_intelligence.research.experiments import Experiments
        return Experiments(self.ws).shadow(request)
    def CanaryPromote(self,request):
        from agent_research_intelligence.research.experiments import Experiments
        return Experiments(self.ws).canary(request)
    def Rollback(self,request):
        from agent_research_intelligence.research.experiments import Experiments
        return Experiments(self.ws).rollback(request)
    def AuditAppend(self,actor,rq_id,action,source,basis,effect):
        if effect not in ('READ','ANALYZE','PROPOSE','FAILED','NO_CHANGE'):raise BoundaryError('No production effect authority')
        self.ResearchQuestionRead(rq_id);self.catalog.validate_basis([Basis.model_validate(b) for b in basis])
        return self.catalog.put('audit',dict(actor=actor,rq_id=rq_id,action=action,source=source,basis=basis,effect=effect),reason='Explicit audit event')


NAMES=tuple(name for name in ResearchContracts.__dict__ if name[0].isupper())


def invoke(workspace,name,request):
    if name not in NAMES:raise BoundaryError('Unknown Versioned contract')
    if name=='AudioExtract':raise BoundaryError('AudioExtract is internal to a leased pipeline; use youtube-research for managed acquisition')
    return getattr(ResearchContracts(workspace),name)(**request)

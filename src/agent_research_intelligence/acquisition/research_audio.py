"""Selected YouTube candidate -> existing isolated audio/Parakeet pipeline."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from uuid import uuid4

from agent_research_intelligence.acquisition.youtube import video_id
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.connectors.public_http import AcquisitionError


def validate_selection(ws,job):
    if job.get('purpose') not in ('VERSIONED_RQ_AUDIO','VERSIONED_RQ_FRAME'):raise BoundaryError('YTDLP_SAMPLE_NOT_APPROVED')
    path=job.get('selection_artifact','')
    if not path.startswith('data/youtube/research/') or not path.endswith('/selection.json'):
        raise BoundaryError('RQ_SELECTION_REQUIRED')
    raw=ws.read(path)
    if hashlib.sha256(raw).hexdigest()!=job.get('selection_sha256'):raise BoundaryError('RQ_SELECTION_HASH_MISMATCH')
    selection=json.loads(raw)
    if selection.get('scope')!='VERSIONED_V0_1_PUBLIC_RESEARCH' or not selection.get('rq_id'):
        raise BoundaryError('RQ_SELECTION_SCOPE_REQUIRED')
    if (job['video_id'] not in selection.get('video_ids',[]) or video_id(job['source_url'])!=job['video_id']):
        raise BoundaryError('RQ_SELECTION_VIDEO_MISMATCH')
    return selection


def transcribe_video(ws,url,*,selection_artifact,selection_sha256,caption_failure,material_type,
                     profile_id,offset_s=0,duration_s=None,diarization_provider=None):
    from agent_research_intelligence.acquisition.parakeet_profile import PROFILE_ID
    if profile_id!=PROFILE_ID or material_type!='ENGLISH_TECHNICAL_LECTURE':
        raise BoundaryError('EXPLICIT_LIMITED_ENGLISH_PROFILE_AND_MATERIAL_TYPE_REQUIRED')
    if caption_failure not in ('NO_CAPTIONS_AUDIO_GATE_REQUIRED','REQUESTED_CAPTION_LANGUAGE_UNAVAILABLE',
                               'EMPTY_CAPTION_RESPONSE','CAPTION_PARSE_FAILED'):
        raise BoundaryError('CAPTION_FAILURE_NOT_ELIGIBLE_FOR_AUDIO_FALLBACK')
    import psutil
    from agent_research_intelligence.acquisition.audio_runtime import Budget,heavy_slot,run_worker,clean_media
    from agent_research_intelligence.acquisition.caption_inventory import inspect_video
    from agent_research_intelligence.acquisition.media_http import AudioHTTP
    from agent_research_intelligence.connectors.youtube_proxy import APPROVED_PROXY
    from agent_research_intelligence.acquisition.asr_profile import input_identity
    from agent_research_intelligence.acquisition.parakeet_audio import run_parakeet_audio
    job_id=uuid4().hex;prefix='data/audio/'+job_id;temp='tmp/media/'+job_id
    job={'job_id':job_id,'video_id':video_id(url),'source_url':url,'purpose':'VERSIONED_RQ_AUDIO',
         'selection_artifact':selection_artifact,'selection_sha256':selection_sha256,
         'offset_s':offset_s,'duration_s':duration_s,'language':'en','speakers':-1}
    validate_selection(ws,job);budget=Budget(ws,psutil);budget.check(force=True)
    owned=False;result={'state':'RUNNING','caption_failure':caption_failure,'model_runs':0}
    def save(name,data):ws.write(prefix+'/'+name,canonical(data))
    try:
        with heavy_slot(ws):
            receipt,_=inspect_video(AudioHTTP(APPROVED_PROXY),url)
            save('caption-inventory.json',receipt)
            length=float(receipt['duration_s'])
            import math
            if duration_s is None:duration_s=min(600,length-offset_s)
            if not all(type(v) in (int,float) and math.isfinite(v) for v in (offset_s,duration_s)) or offset_s<0 or not 0<duration_s<=1800 or offset_s+duration_s>length:
                raise BoundaryError('Invalid research audio interval')
            job['duration_s']=duration_s;save('job.json',job)
            ws.write(temp+'/owned.json',canonical({'job_id':job_id,'purpose':'AUDIO_TEMP_MEDIA',
                'supervisor_pid':os.getpid(),'supervisor_created':psutil.Process().create_time()}));owned=True
            from agent_research_intelligence.research.contracts import ResearchContracts
            media=ResearchContracts(ws).AudioExtract(job_id,budget);save('audio-receipt.json',media)
            run_worker(ws,job_id,'normalize',budget,timeout_s=300)
            identity=input_identity(ws.checked_path(temp+'/normalized.wav'))
            source={'audio':temp+'/normalized.wav','source_id':job['video_id'],'source_url':url,
                'offset_s':offset_s,'duration_s':duration_s,'language':'en','material_type':material_type,
                'sha256':identity['normalized_wav_sha256'],'pcm_sha256':identity['pcm_frames_sha256'],
                'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                'acquisition_basis':'SELECTED_RQ_VIDEO_CAPTION_FALLBACK',
                # Available but unusable captions are not misrepresented as absent.
                'caption_status':'NO_USABLE_CAPTION' if receipt['status']=='NO_USABLE_CAPTION' else 'UNKNOWN',
                'caption_inventory':prefix+'/caption-inventory.json',
                'caption_inventory_sha256':hashlib.sha256(ws.read(prefix+'/caption-inventory.json')).hexdigest(),
                'caption_failure':caption_failure}
            save('prepared-source.json',source)
        # Release media slot before the existing inference supervisor takes it.
        result=ResearchContracts(ws).AudioTranscribe(source,diarization_provider=diarization_provider)
        if result['state']!='EVIDENCE_READY_QUALITY_UNASSESSED':raise AcquisitionError(result.get('error','ASR_FALLBACK_BLOCKED'))
        return result
    except BaseException as e:
        result={**result,'state':'BLOCKED','error':getattr(e,'code',type(e).__name__)}
        raise
    finally:
        result['media_removed']=clean_media(ws,job_id) if owned else []
        save('research-fallback-result.json',result)


def diarize_caption_video(ws,row,segments):
    """Explicit Community-1 for captioned material. Captions stay the text source.

    Uses the same first <=600 s bounded interval as the normal research fallback;
    captions outside that interval remain present with UNKNOWN attribution.
    """
    if row.get('diarization_provider')!='community1':raise BoundaryError('EXPLICIT_COMMUNITY1_REQUIRED')
    import psutil
    from agent_research_intelligence.acquisition.audio_runtime import Budget,heavy_slot,run_worker,clean_media
    from agent_research_intelligence.acquisition.asr_profile import input_identity
    from agent_research_intelligence.acquisition.identity_resolution import resolve_speakers
    job_id=uuid4().hex;prefix='data/audio/'+job_id;temp='tmp/media/'+job_id
    duration=min(600,row['metadata']['duration_s'])
    job={'job_id':job_id,'video_id':row['video_id'],'source_url':row['url'],'purpose':'VERSIONED_RQ_AUDIO',
        'selection_artifact':row['selection_artifact'],'selection_sha256':row['selection_sha256'],
        'offset_s':0,'duration_s':duration,'speakers':-1,'diarization_provider':'community1','language':None}
    validate_selection(ws,job);budget=Budget(ws,psutil);owned=False
    result={'status':'RUNNING','ASR_runs':0,'diarization_runs':0,'caption_text_modified':False}
    try:
        with heavy_slot(ws):
            budget.check(force=True)
            if psutil.virtual_memory().available<12*2**30:raise AcquisitionError('AUDIO_INTEGRATION_START_RESOURCE_WAIT_12GIB')
            ws.write(prefix+'/job.json',canonical(job))
            ws.write(temp+'/owned.json',canonical({'job_id':job_id,'purpose':'AUDIO_TEMP_MEDIA','supervisor_pid':os.getpid(),
                'supervisor_created':psutil.Process().create_time()}));owned=True
            from agent_research_intelligence.research.contracts import ResearchContracts
            result['media']=ResearchContracts(ws).AudioExtract(job_id,budget)
            run_worker(ws,job_id,'normalize',budget,timeout_s=300)
            identity=input_identity(ws.checked_path(temp+'/normalized.wav'))
            job['normalized_sha256']=identity['normalized_wav_sha256'];ws.write(prefix+'/job.json',canonical(job),replace=True)
            run_worker(ws,job_id,'diarize',budget)
            result['raw']=json.loads(ws.read(temp+'/diarize.json'))
            ws.write(prefix+'/diarize.json',canonical(result['raw']))
            turns=result['raw']['raw_segments'];associations=[]
            for index,s in enumerate(segments):
                start=s['start'];end=start+s.get('duration',0)
                active=[t for t in turns if min(end,t['end'])>max(start,t['start'])]
                labels={t['speaker_id'] for t in active}
                # Captions lack native word times: only a fully covered,
                # nonoverlapping cue may get a candidate anonymous label.
                label=next(iter(labels)) if len(labels)==1 and any(t['start']<=start<end<=t['end'] for t in active) and end<=duration else None
                associations.append({**s,'caption_index':index,'speaker_id':label,'assignment_status':'ASSIGNED' if label else 'UNKNOWN',
                                     'assignment_basis':'FULL_CUE_SINGLE_TURN_CANDIDATE','overlap_labels':sorted(labels)})
            result.update(status='CANDIDATE_ASSOCIATION',diarization_runs=1,coverage_original_s=[0,duration],
                segments=associations,identity=resolve_speakers(turns,associations,row.get('identity_roster',[])),
                input=identity,limitations=['Caption cue association is not word-level alignment or human identity confirmation. Outside coverage remains UNKNOWN.'])
            return {**result,'artifact':prefix+'/caption-speakers.json'}
    except BaseException as e:
        result.update(status='BLOCKED',error=getattr(e,'code',type(e).__name__));raise
    finally:
        result['media_removed']=clean_media(ws,job_id) if owned else []
        ws.write(prefix+'/caption-speakers.json',canonical(result))

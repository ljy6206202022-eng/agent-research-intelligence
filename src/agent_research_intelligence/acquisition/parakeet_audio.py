"""Explicit limited-scope normal audio entry, live or sealed-output replay."""
from datetime import datetime,timezone
import json,math,os
from uuid import uuid4
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import verify_policy
from agent_research_intelligence.acquisition.asr_profile import file_hash,input_identity
from agent_research_intelligence.acquisition.audio_runtime import Budget,heavy_slot,clean_media,run_worker
from agent_research_intelligence.acquisition.parakeet_profile import PROFILE_ID,PROFILE_SHA256,load_profile
from agent_research_intelligence.acquisition.parakeet_output import make_output,reading_markdown
from agent_research_intelligence.acquisition.parakeet_runtime import run_parakeet_worker
from agent_research_intelligence.connectors.public_http import AcquisitionError


def check_source(source):
    if not all(type(source.get(k)) in (int,float) and math.isfinite(source[k]) for k in ['offset_s','duration_s']) or source['offset_s']<0 or not 0<source['duration_s']<=1800:raise BoundaryError('INVALID_SOURCE_TIMELINE')
    if source.get('language')!='en':raise BoundaryError('ENGLISH_INPUT_REQUIRED')
    if not all(isinstance(source.get(k),str) and source[k] for k in ['source_id','source_url','acquisition_basis']):raise BoundaryError('SOURCE_PROVENANCE_REQUIRED')
    if source.get('material_type','UNKNOWN') not in ['UNKNOWN','ENGLISH_TECHNICAL_LECTURE','COMPLEX_OVERLAPPING_MEETING','NORMAL_CONVERSATION']:raise BoundaryError('INVALID_MATERIAL_TYPE')
    if source.get('caption_status','UNKNOWN') not in ['UNKNOWN','CAPTIONS_AVAILABLE','NO_USABLE_CAPTION']:raise BoundaryError('INVALID_CAPTION_STATUS')
    if source.get('caption_status')=='NO_USABLE_CAPTION':
        # A caller's filename/claim cannot establish caption absence.
        if not source.get('caption_inventory') or not source.get('caption_inventory_sha256'):raise BoundaryError('NO_CAPTION_CLAIM_REQUIRES_INVENTORY')


def verify_live_source(ws,source):
    check_source(source)
    if not isinstance(source.get('audio'),str) or not source['audio'].startswith(('data/annotation/','validation/','tmp/media/')):raise BoundaryError('AUDIO_OUTSIDE_OWNED_MEDIA')
    expiry=datetime.fromisoformat(source['expires_at'])
    if expiry.tzinfo is None or datetime.now(timezone.utc)>=expiry:raise BoundaryError('INPUT_LEASE_EXPIRED')
    path=ws.checked_path(source['audio']);identity=input_identity(path)
    if identity['normalized_wav_sha256']!=source.get('sha256') or (identity['sample_rate'],identity['channels'],identity['bit_depth'])!=(16000,1,16) or abs(identity['frames']/16000-source['duration_s'])>.01:raise BoundaryError('AUDIO_IDENTITY_MISMATCH')
    if source.get('pcm_sha256') and identity['pcm_frames_sha256']!=source['pcm_sha256']:raise BoundaryError('PCM_IDENTITY_MISMATCH')
    return path,identity


def checked_json(ws,path,digest):
    p=ws.checked_path(path)
    if not digest or file_hash(p)!=digest:raise BoundaryError('SEALED_EVIDENCE_HASH_MISMATCH')
    return json.loads(p.read_text())


def run_parakeet_audio(ws,*,source=None,replay=None,withhold_captions=False,diarization_provider=None,diarization_reuse=None,alignment='ordinary'):
    verify_policy(ws);profile=load_profile(ws)
    if alignment not in ('ordinary','exclusive'):raise BoundaryError('ALIGNMENT_MODE_INVALID')
    if diarization_provider not in (None,'community1'):raise BoundaryError('PARAKEET_DIARIZATION_REQUIRES_COMMUNITY1')
    if diarization_reuse and diarization_provider!='community1':raise BoundaryError('REUSE_REQUIRES_EXPLICIT_COMMUNITY1')
    if alignment=='exclusive' and diarization_reuse is None:raise BoundaryError('EXCLUSIVE_REQUIRES_SAVED_NATIVE_PAIR')
    if replay is not None:
        if source is not None:raise BoundaryError('ONE_INPUT_MODE_REQUIRED')
        source=replay['source']
    check_source(source)
    if source.get('caption_status')=='NO_USABLE_CAPTION':
        inv=checked_json(ws,source['caption_inventory'],source['caption_inventory_sha256'])
        if inv.get('status')!='NO_USABLE_CAPTION' or inv.get('video_id')!=source['source_id']:raise BoundaryError('NO_CAPTION_INVENTORY_MISMATCH')
    if source.get('caption_status')=='CAPTIONS_AVAILABLE' and not withhold_captions:
        return {'state':'CAPTIONS_PREFERRED','quality':'UNASSESSED','model_runs':0,'reason':'Use existing caption acquisition first; an explicit test-only withholding flag is required to bypass.','source':source}
    id=uuid4().hex;prefix='data/audio/'+id;temp='tmp/media/'+id;owned=False
    def persist(name,x):ws.write(prefix+'/'+name,json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False).encode())
    result={'job_id':id,'state':'RUNNING','quality':'UNASSESSED','scope':'LIMITED_ENGLISH_TECHNICAL_LECTURE','profile_id':PROFILE_ID,'model_runs':0,'diarization_runs':0,'source_type_is_not_quality_verdict':True}
    persist('source.json',source);persist('profile.json',profile)
    try:
        if replay is not None:
            raw=checked_json(ws,replay['raw_api'],replay['raw_api_sha256']);binding=checked_json(ws,replay['input_binding'],replay['input_binding_sha256']);identity=binding['identity']
            effective=checked_json(ws,replay['effective_config'],replay['effective_config_sha256'])
            if effective!=profile['configuration'] or source.get('sha256')!=identity['normalized_wav_sha256']:raise BoundaryError('REPLAY_PROFILE_OR_INPUT_MISMATCH')
            if (identity['sample_rate'],identity['channels'],identity['bit_depth'])!=(16000,1,16) or abs(identity['frames']/16000-source['duration_s'])>.01:raise BoundaryError('REPLAY_TIMELINE_MISMATCH')
            classification='REUSED_HISTORICAL_EVIDENCE';persist('replay-binding.json',replay);ws.write(prefix+'/raw-api.json',ws.read(replay['raw_api']))
            if diarization_provider and not diarization_reuse:raise BoundaryError('REPLAY_NEVER_LAUNCHES_A_MODEL')
        else:
            import psutil
            if 'asr' in source:raise BoundaryError('USE_EXPLICIT_REPLAY_MANIFEST')
            with heavy_slot(ws):
                budget=Budget(ws,psutil);budget.check(force=True);audio,identity=verify_live_source(ws,source);load_profile(ws,assets=True)
                job={'job_id':id,'purpose':'PARAKEET_LIMITED_AUDIO','asr_profile_id':PROFILE_ID,'model_directory':profile['model']['directory'],'expected_input':identity,'offset_s':source['offset_s'],'duration_s':source['duration_s'],'language':'en','source_id':source['source_id']}
                persist('job.json',job);persist('asr-job.json',job)
                ws.write(temp+'/owned.json',json.dumps({'job_id':id,'purpose':'AUDIO_TEMP_MEDIA','supervisor_pid':os.getpid(),'supervisor_created':psutil.Process().create_time()}).encode());owned=True
                _,again=verify_live_source(ws,source)
                if again!=identity:raise BoundaryError('SOURCE_CHANGED')
                body=audio.read_bytes()
                import hashlib
                if hashlib.sha256(body).hexdigest()!=identity['normalized_wav_sha256']:raise BoundaryError('SOURCE_COPY_CHANGED')
                ws.write(temp+'/normalized.wav',body);persist('input.json',{'identity':identity,'source_offset_s':source['offset_s']})
                result['model_invocation_attempts']=1;run_parakeet_worker(ws,id,budget);result['model_runs']=1
                raw=json.loads(ws.read(temp+'/raw-api.json'));classification='REAL_THIS_JOB'
                if diarization_provider=='community1' and not diarization_reuse:
                    job.update(diarization_provider='community1',normalized_sha256=identity['normalized_wav_sha256'])
                    ws.write(prefix+'/job.json',json.dumps(job).encode(),replace=True)
                    run_worker(ws,id,'diarize',budget);result['diarization_runs']=1
                    d=json.loads(ws.read(temp+'/diarize.json'));persist('diarize.json',d)
                    pair={'input_sha256':d['input_sha256'],'source_offset_s':source['offset_s'],'model_commit':d['model_commit'],'normal':d['raw_segments'],'same_output_object':False,'reference_mapping_used':False,'classification':'REAL_THIS_JOB_ORDINARY_ONLY'}
        if diarization_reuse:
            pair=checked_json(ws,diarization_reuse['path'],diarization_reuse['sha256'])
            from agent_research_intelligence.acquisition.community1 import manifest
            if pair['model_commit']!=manifest(ws)['commit']:raise BoundaryError('DIARIZATION_MODEL_MISMATCH')
            persist('diarization-reuse.json',diarization_reuse)
        elif not diarization_provider:pair=None
        transcript,reading=make_output(raw,identity,source,profile,classification,pair,alignment)
        transcript.update(profile_sha256=PROFILE_SHA256,caption_policy='TEST_WITHHELD_AVAILABLE_CAPTIONS' if source.get('caption_status')=='CAPTIONS_AVAILABLE' and withhold_captions else 'CAPTIONS_PRIORITY_UNKNOWN_OR_ABSENT',input_audio_present_this_job=replay is None)
        persist('transcript.json',transcript);persist('reading.json',reading)
        ws.write(prefix+'/RAW.txt',(raw['text']+'\n').encode());ws.write(prefix+'/READING.md',reading_markdown(transcript,reading).encode())
        result.update(state='EVIDENCE_READY_QUALITY_UNASSESSED',asr_classification=classification,words=len(transcript['words']),diarization_status=transcript['diarization_status'],outputs={n:prefix+'/'+n for n in ['raw-api.json','transcript.json','reading.json','RAW.txt','READING.md']})
    except (BoundaryError,AcquisitionError,OSError,ValueError,KeyError,TypeError,AssertionError) as e:
        result.update(state='BLOCKED',error=str(e) if isinstance(e,BoundaryError) else getattr(e,'code',type(e).__name__))
    except BaseException as e:
        result.update(state='INTERRUPTED',error=type(e).__name__);raise
    finally:
        result['cleanup']={'removed':clean_media(ws,id) if owned else [],'policy':'IMMEDIATE_OWN_COPY_CLEANUP','borrowed_source_unchanged_by_tool':True}
        persist('result.json',result)
    return result

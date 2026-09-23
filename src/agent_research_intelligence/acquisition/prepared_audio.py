"""Normal local-input audio job path. Borrow approved PCM; own and clean work copies."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from uuid import uuid4
import wave

from agent_research_intelligence.acquisition.asr_profile import load_frozen_profile, load_selected_profile, require_profile_language, input_identity
from agent_research_intelligence.acquisition.audio_runtime import Budget, heavy_slot, run_worker, clean_media
from agent_research_intelligence.acquisition.audio_types import Word, Turn, merge
from agent_research_intelligence.acquisition.community1 import selected, manifest
from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import verify_policy


def verify_source(workspace, source, *, profile_id=None):
    required = {'audio','sha256','source_id','source_url','offset_s','duration_s',
                'language','acquisition_basis','expires_at'}
    if not required <= set(source) or set(source)-required-{'asr','asr_sha256','asr_original_offset_s'}:
        raise BoundaryError('PREPARED_SOURCE_FIELDS_INVALID')
    if (not source['audio'].endswith('.wav') or not source['audio'].startswith(('data/annotation/', 'validation/', 'tmp/media/'))
            or not all(type(source[k]) in (int,float) and math.isfinite(source[k]) for k in ('offset_s','duration_s'))
            or source['offset_s'] < 0 or not 0 < source['duration_s'] <= 1800
            or not all(isinstance(source[k],str) and source[k].strip() for k in ('source_id','source_url','acquisition_basis'))
            or not source['source_url'].startswith('https://')):
        raise BoundaryError('PREPARED_SOURCE_BINDING_INVALID')
    if source['expires_at']:
        expiry = datetime.fromisoformat(source['expires_at'])
        if expiry.tzinfo is None or datetime.now(timezone.utc) >= expiry:
            raise BoundaryError('PREPARED_INPUT_LEASE_EXPIRED')
    audio = workspace.checked_path(source['audio'])
    identity = input_identity(audio)
    if (identity['normalized_wav_sha256'] != source['sha256']
            or (identity['sample_rate'],identity['channels'],identity['bit_depth']) != (16000,1,16)
            or identity['frames']/16000 != source['duration_s']):
        raise BoundaryError('PREPARED_INPUT_IDENTITY_MISMATCH')
    profile = load_frozen_profile(workspace) if profile_id is None else load_selected_profile(workspace, profile_id)
    require_profile_language(profile, source['language'])
    asr = None
    if 'asr' in source:
        body = workspace.read(source['asr'])
        if hashlib.sha256(body).hexdigest() != source.get('asr_sha256'):
            raise BoundaryError('REUSED_ASR_HASH_MISMATCH')
        asr = json.loads(body)
        if (asr.get('provider') != 'faster-whisper' or asr.get('input') != identity
                or asr.get('ASR_RUN_CONFIG_HASH') != profile['ASR_RUN_CONFIG_HASH']
                or source.get('asr_original_offset_s') != source['offset_s']):
            raise BoundaryError('REUSED_ASR_INPUT_PROFILE_OR_TIMELINE_MISMATCH')
    return audio, identity, asr


def run_prepared_audio(workspace, *, source, diarization_provider):
    import psutil
    verify_policy(workspace)
    if selected({'diarization_provider':diarization_provider}) != 'community1':
        raise BoundaryError('PREPARED_COMMUNITY1_REQUIRES_EXPLICIT_SELECTION')
    manifest(workspace)
    audio, identity, reused_asr = verify_source(workspace, source)
    job_id = uuid4().hex
    prefix = 'data/audio/'+job_id
    temp = 'tmp/media/'+job_id
    def persist(name, value):
        workspace.write(prefix+'/'+name,json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode())
    job = {'job_id':job_id,'video_id':source['source_id'],'source_url':source['source_url'],
           'offset_s':source['offset_s'],'duration_s':source['duration_s'],'speakers':-1,
           'language':source['language'],'diarization_provider':'community1',
           'purpose':'AUDIO_PREPARED_AUDIO_INTEGRATION','acquisition_basis':source['acquisition_basis'],
           'normalized_sha256':identity['normalized_wav_sha256']}
    persist('job.json',job)
    persist('source-binding.json',source)
    result = {'job_id':job_id,'state':'RUNNING','authority':'EXTERNAL_EVIDENCE','quality':'UNASSESSED',
              'diarization_provider':'community1','fallback':'DISABLED','stages':[]}
    budget = Budget(workspace,psutil)
    owned_media = False
    with heavy_slot(workspace):
        try:
            budget.check(force=True)
            if psutil.virtual_memory().available < 12*2**30:
                raise AcquisitionError('AUDIO_INTEGRATION_START_RESOURCE_WAIT_12GIB')
            workspace.write(temp+'/owned.json',json.dumps({'job_id':job_id,'purpose':'AUDIO_TEMP_MEDIA',
                'supervisor_pid':os.getpid(),'supervisor_created':psutil.Process(os.getpid()).create_time()}).encode())
            owned_media = True
            body = workspace.read(source['audio'],limit=60*1024**2)
            if hashlib.sha256(body).hexdigest() != source['sha256']:
                raise BoundaryError('BORROWED_INPUT_CHANGED')
            workspace.write(temp+'/normalized.wav',body)
            persist('normalize.json',{**identity,'duration_s':source['duration_s'],
                                     'original_offset_s':source['offset_s'],'method':'EXACT_APPROVED_PCM_COPY_NO_RENORMALIZATION'})
            if reused_asr is None:
                result['stages'].append(run_worker(workspace,job_id,'asr',budget))
                asr = json.loads(workspace.read(temp+'/asr.json'))
                asr_class = 'REAL'
            else:
                asr, asr_class = reused_asr, 'REUSED_HISTORICAL_EVIDENCE'
            persist('asr.json',asr)
            result['stages'].append(run_worker(workspace,job_id,'diarize',budget))
            diarization = json.loads(workspace.read(temp+'/diarize.json'))
            if diarization.get('provider') != 'community1' or diarization.get('input_sha256') != source['sha256']:
                raise BoundaryError('DIARIZATION_INPUT_OR_PROVIDER_MISMATCH')
            persist('diarize.json',diarization)
            words = [Word(w['start'],w['end'],w['word'],w.get('probability'))
                     for segment in asr['segments'] for w in (segment['words'] or [])]
            transcript = merge(words,[Turn(**turn) for turn in diarization['turns']],
                               offset_s=source['offset_s'],duration_s=source['duration_s'])
            transcript.update(source_id=source['source_id'],source_url=source['source_url'],
                normalized_audio_sha256=source['sha256'],asr_profile_hash=asr['ASR_RUN_CONFIG_HASH'],
                asr_classification=asr_class,diarization_classification='REAL',
                execution='LIVE_DIARIZATION_WITH_REUSED_REAL_ASR' if reused_asr is not None else 'LIVE_ASR_AND_DIARIZATION',
                asr_evidence=prefix+'/asr.json',diarization_raw_evidence=prefix+'/diarize.json')
            persist('transcript.json',transcript)
            result.update(state='EVIDENCE_READY_QUALITY_UNASSESSED',words=len(words),
                          speaker_count=len({t['label'] for t in diarization['turns']}),
                          asr_classification=asr_class,identity_resolution='NOT_PERFORMED')
        except (AcquisitionError,BoundaryError,OSError,ValueError,KeyError,TypeError) as exc:
            result.update(state='BLOCKED',error=getattr(exc,'code',str(exc) if isinstance(exc,BoundaryError) else type(exc).__name__))
        except BaseException as exc:
            result.update(state='INTERRUPTED',error=type(exc).__name__)
            raise
        finally:
            try:
                result['media_cleanup']={'status':'PASS','removed':clean_media(workspace,job_id) if owned_media else [],
                    'borrowed_original_untouched':True,'retention':'IMMEDIATE_OWNED_COPY_CLEANUP'}
            except (OSError,BoundaryError) as exc:
                result.update(state='BLOCKED',cleanup_error=type(exc).__name__)
                result['media_cleanup']={'status':'FAILED','borrowed_original_untouched':True}
            persist('cleanup.json',result['media_cleanup'])
            persist('result.json',result)
    return result

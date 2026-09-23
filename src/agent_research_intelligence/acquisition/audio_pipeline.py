"""audio-only pipeline; external evidence, no research/production authority promotion."""
import json
import math
import os
import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from agent_research_intelligence.acquisition.caption_inventory import inspect_video
from agent_research_intelligence.acquisition.media_http import AudioHTTP
from agent_research_intelligence.acquisition.media_provider import MediaContext, acquire_media
from agent_research_intelligence.acquisition.audio_runtime import Budget, heavy_slot, run_worker, clean_media, reap_orphaned_media
from agent_research_intelligence.acquisition.audio_types import Word, Turn, merge
from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.connectors.youtube_proxy import APPROVED_PROXY
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import verify_policy


def run_audio(workspace, *, url, purpose, offset_s, duration_s, speakers=-1, language=None, acquisition_basis,
              diarization_provider='sherpa-onnx'):
    import psutil  # Only installed in the independent audio environment.
    verify_policy(workspace)
    from agent_research_intelligence.acquisition.community1 import selected
    selected({'diarization_provider':diarization_provider,'speakers':speakers})
    if purpose not in ('NO_SUBTITLE','MULTIPLE_SPEAKER'):
        raise BoundaryError('Only audio acceptance purposes enabled')
    if not all(type(x) in (int,float) and math.isfinite(x) for x in (offset_s,duration_s)) or offset_s < 0 or not 0 < duration_s <= 1800:
        raise BoundaryError('Audio range must be finite, nonnegative and at most 30 minutes')
    if type(speakers) is not int or speakers != -1:
        raise BoundaryError('Bare speaker count denied; use a sealed scoped context job')
    if language not in (None,'en','zh') or not isinstance(acquisition_basis,str) or not acquisition_basis.strip():
        raise BoundaryError('Expected language and explicit acquisition basis')
    identifier = uuid4().hex
    prefix = 'data/audio/'+identifier
    result = {'job_id':identifier,'state':'RUNNING','at':datetime.now(timezone.utc).isoformat(),
              'purpose':purpose,'authority':'EXTERNAL_EVIDENCE','quality':'UNASSESSED','stages':[]}
    budget = Budget(workspace,psutil)
    http = AudioHTTP(APPROVED_PROXY)

    def persist(name, value):
        workspace.write(prefix+'/'+name,json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode(),replace=True)

    with heavy_slot(workspace):
        result['orphan_media_reaped']=reap_orphaned_media(workspace,psutil)
        workspace.write(f'tmp/media/{identifier}/owned.json',json.dumps({'job_id':identifier,'purpose':'AUDIO_TEMP_MEDIA',
                        'supervisor_pid':os.getpid(),'supervisor_created':psutil.Process(os.getpid()).create_time()}).encode())
        try:
            budget.check(force=True)
            receipt, player = inspect_video(http,url)
            persist('caption-inventory.json',receipt)
            result['stages'].append({'stage':'CAPTION_INVENTORY','status':receipt['status']})
            if purpose == 'NO_SUBTITLE' and receipt['status'] != 'NO_USABLE_CAPTION':
                raise AcquisitionError('SAMPLE_HAS_CAPTIONS_DO_NOT_CLAIM_NO_SUBTITLE')
            length = float(receipt['duration_s'])
            if offset_s + duration_s > length:
                raise BoundaryError('Requested interval exceeds video timeline')
            # Check the separate gate BEFORE media/compute work. No automatic downloads.
            try:
                model_gate = json.loads(workspace.read('config/permissions/audio-models.json'))
            except (FileNotFoundError, OSError, BoundaryError):
                raise AcquisitionError('MODEL_DOWNLOAD_GATE_CLOSED')
            if model_gate.get('status') != 'USER_APPROVED' or model_gate.get('purpose') != 'AUDIO_LOCAL_AUDIO_ONLY':
                raise AcquisitionError('MODEL_DOWNLOAD_GATE_CLOSED')
            job = {'job_id':identifier,'video_id':receipt['video_id'],'source_url':receipt['url'],
                   'offset_s':offset_s,'duration_s':duration_s,'speakers':speakers,'language':language,
                   'diarization_provider':diarization_provider,
                   'purpose':purpose,'acquisition_basis':acquisition_basis,'source_classification':'PUBLIC'}
            sources=[*workspace.root.joinpath('src/agent_research_intelligence').rglob('*.py'),workspace.root/'environments/audio/worker.py',
                     workspace.root/'environments/audio/uv.lock',workspace.root/'config/permissions/audio-models.json']
            if diarization_provider == 'community1':
                sources.extend(workspace.root/p for p in ('environments/pyannote_candidate/integration_worker.py',
                    'environments/pyannote_candidate/pylock.toml','config/permissions/audio-community1.json'))
            job['implementation_hashes']={str(p.relative_to(workspace.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources if p.is_file()}
            persist('job.json',job)
            media = acquire_media(MediaContext(workspace,identifier,receipt,player,http,budget,persist,purpose))
            persist('audio-receipt.json',media)
            result['stages'].append({'stage':'AUDIO_ACQUISITION','status':'REAL_PASS'})
            for stage in ('normalize','asr','diarize'):
                resources = run_worker(workspace,identifier,stage,budget,timeout_s=300 if stage=='normalize' else 3600)
                output = json.loads(workspace.read(f'tmp/media/{identifier}/{stage}.json'))
                persist(stage+'.json',output)
                result['stages'].append(resources)
            asr = json.loads(workspace.read(prefix+'/asr.json'))
            diarization = json.loads(workspace.read(prefix+'/diarize.json'))
            normalized = json.loads(workspace.read(prefix+'/normalize.json'))
            if diarization_provider == 'community1' and (
                    diarization.get('provider') != 'community1'
                    or diarization.get('input_sha256') != asr.get('input',{}).get('normalized_wav_sha256')):
                raise BoundaryError('DIARIZATION_ASR_INPUT_OR_PROVIDER_MISMATCH')
            words = [Word(w['start'],w['end'],w['word'],w.get('probability')) for segment in asr['segments'] for w in segment['words']]
            merged = merge(words,[Turn(**t) for t in diarization['turns']],offset_s=offset_s,duration_s=normalized['duration_s'])
            merged.update(video_id=job['video_id'],source_url=job['source_url'],audio_sha256=media['sha256'],
                          model_manifest=model_gate,caption_inventory=receipt['status'],
                          diarization_provider=diarization_provider,
                          diarization_raw_evidence=prefix+'/diarize.json')
            persist('transcript.json',merged)
            result.update(state='EVIDENCE_READY_QUALITY_UNASSESSED',words=len(words),
                          speaker_count=len({t['label'] for t in diarization['turns']}),
                          original_timeline=True,identity_resolution='NOT_PERFORMED')
        except (AcquisitionError,BoundaryError,OSError,ValueError,KeyError,TypeError) as exc:
            result.update(state='BLOCKED',error=getattr(exc,'code',type(exc).__name__))
        except BaseException as exc:
            result.update(state='INTERRUPTED',error=type(exc).__name__)
            raise
        finally:
            try:
                removed = clean_media(workspace,identifier)
                result['media_cleanup'] = {'status':'PASS','removed':removed,'retention':'IMMEDIATE_ON_SUCCESS_OR_FAILURE'}
                persist('cleanup.json',result['media_cleanup'])
                if 'media' in locals():
                    media['cleanup_status']='CLEANED'
                    persist('audio-receipt.json',media)
            except (OSError,BoundaryError) as exc:
                result['state']='BLOCKED'; result['media_cleanup']={'status':'FAILED','error':type(exc).__name__}
            persist('transport.json',http.diagnostics())
            persist('result.json',result)
    return result

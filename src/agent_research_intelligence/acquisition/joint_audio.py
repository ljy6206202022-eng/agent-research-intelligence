"""One ASR-only job joined to sealed same-input diarization; no provider rerun."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from agent_research_intelligence.acquisition.asr_profile import ENGLISH_PROFILE_ID, load_selected_profile
from agent_research_intelligence.acquisition.audio_runtime import Budget, heavy_slot, run_worker, clean_media
from agent_research_intelligence.acquisition.audio_types import Word, Turn, merge
from agent_research_intelligence.acquisition.community1 import adapt, manifest
from agent_research_intelligence.acquisition.prepared_audio import verify_source
from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import verify_policy


def verify_reused_diarization(workspace, source, identity, reuse):
    """Bind original predictor bytes, model and timeline; never read scorer mapping."""
    if (reuse.get('provider') != 'community1'
            or reuse.get('classification') != 'REUSED_HISTORICAL_EVIDENCE'
            or reuse.get('reference_mapping_used') is not False
            or reuse.get('source_id') != source['source_id']
            or reuse.get('interval') != [source['offset_s'], source['offset_s'] + source['duration_s']]
            or reuse.get('input_sha256') != identity['normalized_wav_sha256']
            or reuse.get('pcm_sha256') != identity['pcm_frames_sha256']):
        raise BoundaryError('REUSED_DIARIZATION_INPUT_BINDING_MISMATCH')
    keys = {'raw','provider_job','prediction_complete','normalized_input','retention_lease'}
    if set(reuse.get('paths', {})) != keys or set(reuse.get('hashes', {})) != keys:
        raise BoundaryError('REUSED_DIARIZATION_MANIFEST_FIELDS')
    values = {}
    for key, relative in reuse['paths'].items():
        body = workspace.read(relative)
        if hashlib.sha256(body).hexdigest() != reuse['hashes'][key]:
            raise BoundaryError('REUSED_DIARIZATION_FILE_DRIFT')
        values[key] = json.loads(body)
    job, completed, normalized, lease = (values[k] for k in
        ('provider_job','prediction_complete','normalized_input','retention_lease'))
    model = manifest(workspace)
    if (reuse.get('model_commit') != model['commit']
            or Path(job.get('model_dir', '')).resolve() != (workspace.root/model['directory']).resolve()
            or job.get('model_files') != model['files']
            or job.get('mode') != 'COMMUNITY1_OFFICIAL_DEFAULT_AUTO'
            or job.get('audio_sha256') != source['sha256']
            or Path(job.get('audio', '')).resolve() != workspace.checked_path(source['audio']).resolve()
            or completed.get('raw_sha256') != reuse['hashes']['raw']
            or completed.get('output') != 'speaker_diarization'
            or completed.get('exclusive_used') is not False):
        raise BoundaryError('REUSED_DIARIZATION_PROVIDER_PROVENANCE_MISMATCH')
    if (normalized.get('sample') != source['source_id']
            or normalized.get('original_interval') != reuse['interval']
            or normalized.get('offset') != source['offset_s']
            or normalized.get('sha256') != source['sha256']
            or normalized.get('pcm_sha256') != identity['pcm_frames_sha256']
            or (normalized.get('sample_rate'), normalized.get('channels'), normalized.get('sample_width_bytes'), normalized.get('frames'))
                != (identity['sample_rate'], identity['channels'], identity['bit_depth']//8, identity['frames'])
            or lease.get('sha256') != source['sha256']
            or lease.get('expires_at') != source['expires_at']
            or not source['expires_at']):
        raise BoundaryError('REUSED_DIARIZATION_TIMELINE_OR_LEASE_MISMATCH')
    adapted = adapt(values['raw'], duration_s=source['duration_s'])
    if completed.get('labels') != len(adapted['label_adapter']) or not adapted['turns']:
        raise BoundaryError('REUSED_DIARIZATION_COMPLETION_MISMATCH')
    return adapted


def merge_joint(asr, diarization, *, source):
    words = [Word(w['start'], w['end'], w['word'], w.get('probability'))
             for segment in asr['segments'] for w in (segment['words'] or [])]
    turns = [Turn(**t) for t in diarization['turns']]
    result = merge(words, turns, offset_s=source['offset_s'], duration_s=source['duration_s'])
    # The existing merge assigns display letters chronologically. Convert only
    # those display IDs back to the original anonymous raw labels, losslessly.
    raw_by_number = {number: raw for raw, number in diarization['label_adapter'].items()}
    ordered = list(dict.fromkeys(t.label for t in sorted(turns, key=lambda t:t.start)))
    labels = {'Speaker ' + (chr(65+i) if i < 26 else str(i+1)): raw_by_number[label]
              for i, label in enumerate(ordered)}
    for item in [*result['words'], *result['speaker_turns']]:
        if item['speaker_id'] is not None:
            item['speaker_id'] = labels[item['speaker_id']]
    result['display_to_raw_label_adapter'] = labels
    return result


def run_prepared_joint(workspace, *, source, profile_id, reuse):
    import psutil
    verify_policy(workspace)
    if profile_id != ENGLISH_PROFILE_ID or 'asr' in source:
        raise BoundaryError('JOINT_REQUIRES_EXPLICIT_NEW_ENGLISH_ASR')
    profile = load_selected_profile(workspace, profile_id)
    audio, identity, _ = verify_source(workspace, source, profile_id=profile_id)
    diarization = verify_reused_diarization(workspace, source, identity, reuse)
    identifier = uuid4().hex
    prefix, temp = 'data/audio/'+identifier, 'tmp/media/'+identifier
    def persist(name, value):
        workspace.write(prefix+'/'+name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())
    # ASR-only uses the existing ASR budget. No Community-1 worker is requested;
    # its unchanged 12 GiB startup rule is irrelevant to reuse of JSON segments.
    job = {'job_id':identifier,'video_id':source['source_id'],'source_url':source['source_url'],
           'offset_s':source['offset_s'],'duration_s':source['duration_s'],'language':'en',
           'asr_profile_id':profile_id,'purpose':'AUDIO_ENGLISH_ASR_ONLY_REUSE_DIARIZATION',
           'normalized_sha256':source['sha256'],'reused_diarization_provider':'community1'}
    persist('job.json', job); persist('source-binding.json', source)
    persist('reused-diarization-binding.json', reuse)
    result = {'job_id':identifier,'state':'RUNNING','authority':'EXTERNAL_EVIDENCE','quality':'UNASSESSED',
              'asr_classification':'REAL','diarization_classification':'REUSED_HISTORICAL_EVIDENCE',
              'diarization_provider':'community1','diarization_model_invocations':0,'fallback':'DISABLED','stages':[]}
    owned_media = False
    with heavy_slot(workspace):
        try:
            budget = Budget(workspace, psutil); budget.check(force=True)
            # Recheck a borrowed input's identity and live lease immediately before copying.
            verify_source(workspace, source, profile_id=profile_id)
            workspace.write(temp+'/owned.json', json.dumps({'job_id':identifier,'purpose':'AUDIO_TEMP_MEDIA',
                'supervisor_pid':os.getpid(),'supervisor_created':psutil.Process(os.getpid()).create_time()}).encode())
            owned_media = True
            body = workspace.read(source['audio'], limit=60*1024**2)
            if hashlib.sha256(body).hexdigest() != source['sha256']:
                raise BoundaryError('BORROWED_INPUT_CHANGED')
            workspace.write(temp+'/normalized.wav', body)
            persist('normalize.json',{**identity,'duration_s':source['duration_s'],
                'original_offset_s':source['offset_s'],'method':'EXACT_APPROVED_PCM_COPY_NO_RENORMALIZATION'})
            result['stages'].append(run_worker(workspace, identifier, 'asr', budget))
            asr = json.loads(workspace.read(temp+'/asr.json'))
            if (asr.get('input') != identity or asr.get('ASR_RUN_CONFIG_HASH') != profile['ASR_RUN_CONFIG_HASH']
                    or asr.get('profile_id') != profile_id or asr.get('language') != 'en'):
                raise BoundaryError('JOINT_ASR_IDENTITY_MISMATCH')
            persist('asr.json', asr)
            # Revalidate reused raw output after inference; never alter its bytes.
            verified = verify_reused_diarization(workspace, source, identity, reuse)
            if verified != diarization:
                raise BoundaryError('REUSED_DIARIZATION_CHANGED_DURING_ASR')
            persist('diarize.json', {**diarization,'classification':'REUSED_HISTORICAL_EVIDENCE',
                'input_sha256':source['sha256'],'raw_evidence':reuse['paths']['raw'],
                'raw_sha256':reuse['hashes']['raw'],'model_commit':reuse['model_commit']})
            transcript = merge_joint(asr, diarization, source=source)
            transcript.update(source_id=source['source_id'],source_url=source['source_url'],
                normalized_audio_sha256=source['sha256'],asr_profile_hash=profile['ASR_RUN_CONFIG_HASH'],
                asr_classification='REAL',diarization_classification='REUSED_HISTORICAL_EVIDENCE',
                merge_classification='REAL',execution='LIVE_ENGLISH_ASR_WITH_REUSED_COMMUNITY1_SEGMENTS',
                asr_evidence=prefix+'/asr.json',diarization_raw_evidence=reuse['paths']['raw'])
            persist('transcript.json',transcript)
            result.update(state='EVIDENCE_READY_QUALITY_UNASSESSED',words=len(transcript['words']),
                raw_speaker_count=len(diarization['label_adapter']),
                assigned_speaker_count=len({w['speaker_id'] for w in transcript['words'] if w['speaker_id']}),
                identity_resolution='NOT_PERFORMED')
        except (AcquisitionError,BoundaryError,OSError,ValueError,KeyError,TypeError) as exc:
            result.update(state='BLOCKED',error=getattr(exc,'code',str(exc) if isinstance(exc,BoundaryError) else type(exc).__name__))
        except BaseException as exc:
            result.update(state='INTERRUPTED',error=type(exc).__name__)
            raise
        finally:
            try:
                result['media_cleanup']={'status':'PASS','removed':clean_media(workspace,identifier) if owned_media else [],
                    'borrowed_original_untouched':True,'retention':'IMMEDIATE_OWNED_COPY_CLEANUP'}
            except (OSError,BoundaryError) as exc:
                result.update(state='BLOCKED',cleanup_error=type(exc).__name__)
                result['media_cleanup']={'status':'FAILED','borrowed_original_untouched':True}
            persist('cleanup.json',result['media_cleanup']);persist('result.json',result)
    return result

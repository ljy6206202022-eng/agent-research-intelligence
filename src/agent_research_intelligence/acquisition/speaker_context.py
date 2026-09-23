"""Scoped candidate count decisions. No diarization math, identities, or model calls.
Inputs are projected video materials; references/evaluation are never inputs.
The human receipt records an external act, not a claim inferred by this module.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from agent_research_intelligence.governance.paths import BoundaryError, Workspace
from agent_research_intelligence.acquisition.audio_types import interval

POLICY_VERSION = 'AUDIO_CONTEXT_COUNT_CANDIDATE_v0.1'
AUTO = 'AUDIO_ONLY_AUTO'
CONTEXT = 'CONTEXT_ASSISTED'
DEFAULT_ENABLED = False
GRANTS = 'config/permissions/audio-context-candidate.json'
KINDS = {'TITLE', 'DESCRIPTION', 'CAPTION', 'VERSIONED_ASR'}
CLAIMS = {'ROSTER', 'OPENING_INTRO', 'EXACT_INTERVAL_COUNT', 'LOWER_BOUND', 'RANGE', 'IDENTITY_HINT'}
ASR_HASH = 'adb335bced2db2bb8b672f5d5be34da4ddf5d03ca830fe77c1bc07b819ffbad5'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(ok, reason):
    if not ok:
        raise BoundaryError(reason)


def filehash(workspace, relative):
    path = workspace.checked_path(relative)
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024): h.update(block)
    return h.hexdigest()


def binding_for(workspace, job):
    identifier = job['job_id']
    require(isinstance(identifier,str) and re.fullmatch('[a-f0-9]{32}',identifier), 'Invalid context job')
    start, duration = job['offset_s'], job['duration_s']
    interval(start, start + duration)
    source_id = job.get('video_id') or job.get('source_id')
    require(isinstance(source_id,str) and bool(source_id), 'Missing source ID')
    require(isinstance(job.get('source_url'),str) and job['source_url'].startswith('https://'), 'Missing source URL')
    return {'job_id':identifier, 'source_id':source_id, 'source_url':job['source_url'],
            'source_audio_sha256':filehash(workspace,f'tmp/media/{identifier}/source.audio'),
            'normalized_audio_sha256':filehash(workspace,f'tmp/media/{identifier}/normalized.wav'),
            'original_interval':[start,start+duration]}


def parameters(count):
    return {'configured':{'num_clusters':count,'threshold':0.5},
            'effective':{'num_clusters':count,'threshold':0.5 if count == -1 else None,
                         'threshold_used':count == -1,
                         'count_rule':'AUTOMATIC_DISTANCE_CUT' if count == -1 else 'EXACT_COUNT_CUT'},
            'provider':'sherpa-onnx','version':'1.13.8','identity_resolution':'NOT_PERFORMED'}


def decide(binding, bundle, review, *, created_at=None):
    """Pure reducer: no filesystem/network access or reference-answer argument."""
    require(set(bundle)=={'binding','materials','evidence'}, 'Unexpected context bundle fields')
    require(bundle['binding']==binding, 'CONTEXT_INPUT_BINDING_MISMATCH')
    require(binding.get('original_interval')==[0,600], 'SCOPED_EXPERIMENT_REQUIRES_ORIGINAL_0_600')
    require(isinstance(bundle['materials'],list) and len(bundle['materials'])<=32, 'Invalid materials')
    materials={}
    for m in bundle['materials']:
        require(set(m)=={'id','kind','version','source_id','source_sha256','text','asr_profile_hash','asr_input_sha256'}, 'Invalid projected material')
        require(m['source_id']==binding['source_id'], 'MATERIAL_SOURCE_ID_MISMATCH')
        require(m['asr_input_sha256']==binding['normalized_audio_sha256'] if m['kind']=='VERSIONED_ASR' else m['asr_input_sha256'] is None, 'MATERIAL_ASR_INPUT_MISMATCH')
        require(m['kind'] in KINDS and m['id'] not in materials, 'Non-allowlisted or duplicate material')
        require(all(isinstance(m[k],str) and m[k] for k in ['id','version','text']), 'Missing material data')
        require(isinstance(m['source_sha256'],str) and re.fullmatch('[0-9a-f]{64}',m['source_sha256']), 'Invalid source hash')
        require(m['asr_profile_hash']==ASR_HASH if m['kind']=='VERSIONED_ASR' else m['asr_profile_hash'] is None, 'Unfrozen ASR input')
        materials[m['id']]=m
    require(isinstance(bundle['evidence'],list) and len(bundle['evidence'])<=64, 'Invalid evidence')
    ids=set(); eligible=[]; reasons=[]
    for e in bundle['evidence']:
        require(set(e)=={'id','material_id','claim','count','interval','quote','locator','conflicts','uncertainties'}, 'Invalid evidence fields')
        require(isinstance(e['id'],str) and e['id'] and e['id'] not in ids, 'Duplicate evidence ID')
        ids.add(e['id'])
        require(e['claim'] in CLAIMS and e['material_id'] in materials, 'Invalid evidence type or material')
        require(isinstance(e['quote'],str) and e['quote'] and e['quote'] in materials[e['material_id']]['text'], 'Quote not present in material')
        require(isinstance(e['locator'],str) and bool(e['locator']), 'Missing locator')
        require(isinstance(e['conflicts'],list) and isinstance(e['uncertainties'],list), 'Invalid uncertainty fields')
        require(isinstance(e['interval'],list) and len(e['interval'])==2, 'Invalid evidence interval')
        interval(*e['interval'])
        if e['count'] is not None: require(type(e['count']) is int and e['count']>0, 'Invalid count')
        if e['claim']=='EXACT_INTERVAL_COUNT': require(type(e['count']) is int and e['count']>0, 'Exact claim requires count')
        if e['conflicts'] or e['uncertainties']: reasons.append('UNRESOLVED_CONFLICT_OR_UNCERTAINTY')
        if e['claim']=='EXACT_INTERVAL_COUNT' and e['interval']==binding['original_interval']:
            eligible.append(e)
    if isinstance(review,dict) and 'binding' in review:
        require(review['binding']==binding, 'REVIEW_INPUT_BINDING_MISMATCH')
    verified = (isinstance(review,dict) and set(review)=={'bundle_sha256','binding','reviewer','method','reviewed_at','confirmation_ref','verified_evidence_ids','conflicts','uncertainties'}
        and review['bundle_sha256']==digest(bundle) and review['binding']==binding
        and review['method']=='HUMAN_CHECK_OF_ORIGINAL_VIDEO_MATERIAL'
        and all(isinstance(review[k],str) and review[k] for k in ['reviewer','reviewed_at','confirmation_ref'])
        and isinstance(review['verified_evidence_ids'],list) and set(review['verified_evidence_ids'])<=ids
        and review['conflicts']==[] and review['uncertainties']==[])
    if not verified: reasons.append('HUMAN_CHECK_MISSING_OR_UNRESOLVED')
    eligible=[e for e in eligible if verified and e['id'] in review['verified_evidence_ids']]
    counts={e['count'] for e in eligible}
    # Contradictory exact statements anywhere remain unresolved, even if one was selected.
    all_counts={e['count'] for e in bundle['evidence'] if e['claim']=='EXACT_INTERVAL_COUNT' and e['interval']==binding['original_interval']}
    if len(all_counts)>1: reasons.append('CONFLICTING_EXACT_COUNTS')
    count=next(iter(counts)) if len(counts)==1 and not reasons else -1
    if count==-1: reasons.append('NO_VALID_COUNT_EVIDENCE')
    return {'policy_version':POLICY_VERSION,'mode':CONTEXT,'default_enabled':False,
            'binding':binding,'bundle_sha256':digest(bundle),'review_sha256':digest(review),
            'evidence_ids':sorted(ids),'material_hashes':{m['id']:m['source_sha256'] for m in materials.values()},
            'created_at':created_at or datetime.now(timezone.utc).isoformat(),
            'decision':'AUTO' if count==-1 else 'EXACT_COUNT','num_clusters':count,
            'reasons':sorted(set(reasons)) or ['HUMAN_VERIFIED_EXACT_COUNT_FOR_COMPLETE_INPUT_INTERVAL'],
            'parameters':parameters(count),'authority':'EXTERNAL_EVIDENCE'}


def grant_for(workspace, identifier):
    config=json.loads(workspace.read(GRANTS))
    require(config.get('policy_version')==POLICY_VERSION and config.get('default_enabled') is False
            and config.get('status')=='APPROVED_FOR_SCOPED_EXPERIMENT', 'CONTEXT_CANDIDATE_DISABLED')
    grant=config.get('jobs',{}).get(identifier)
    require(isinstance(grant,dict), 'CONTEXT_JOB_NOT_REGISTERED')
    return grant


def seal_decision(workspace, job):
    """Only explicitly registered prepared jobs, after real human receipts exist.
    No helper here creates grants or marks materials HUMAN_VERIFIED.
    """
    binding=binding_for(workspace,job); identifier=job['job_id']; grant=grant_for(workspace,identifier)
    prefix=f'data/audio/{identifier}/context'
    bundle=json.loads(workspace.read(prefix+'/materials.json'))
    review=json.loads(workspace.read(prefix+'/human-review.json'))
    require(grant.get('binding')==binding and grant.get('bundle_sha256')==digest(bundle)
            and grant.get('review_sha256')==digest(review),'CONTEXT_GRANT_BINDING_MISMATCH')
    result=isolated_decision(workspace,binding,bundle,review)
    workspace.write(prefix+'/decision.json',canonical(result))
    workspace.write(prefix+'/seal.json',canonical({'decision_sha256':digest(result),'grant_sha256':digest(grant)}))
    return result


def validate_job(workspace, job):
    """Revalidate independently at supervisor and guarded worker entry points."""
    mode=job.get('diarization_mode',AUTO)
    require(mode in (AUTO,CONTEXT), 'UNKNOWN_DIARIZATION_MODE')
    count=job.get('speakers',-1)
    require(type(count) is int, 'INVALID_BARE_SPEAKER_COUNT')
    if mode==AUTO:
        require(count==-1 and not any(k in job for k in ('context_decision','context_count')), 'BARE_SPEAKER_COUNT_DENIED')
        return {'mode':AUTO,'parameters':parameters(-1)}
    binding=binding_for(workspace,job);identifier=job['job_id'];grant=grant_for(workspace,identifier)
    prefix=f'data/audio/{identifier}/context'
    bundle=json.loads(workspace.read(prefix+'/materials.json'));review=json.loads(workspace.read(prefix+'/human-review.json'))
    record=json.loads(workspace.read(prefix+'/decision.json'));seal=json.loads(workspace.read(prefix+'/seal.json'))
    require(grant.get('binding')==binding and grant.get('bundle_sha256')==digest(bundle)
            and grant.get('review_sha256')==digest(review),'CONTEXT_GRANT_BINDING_MISMATCH')
    require(seal=={'decision_sha256':digest(record),'grant_sha256':digest(grant)}, 'CONTEXT_SEAL_TAMPERED')
    expected=decide(binding,bundle,review,created_at=record.get('created_at'))
    require(record==expected and count==expected['num_clusters'], 'CONTEXT_EFFECTIVE_PARAMETER_MISMATCH')
    return record


def project_video_record(record, *, source_sha256, version, transcript_kind='CAPTION', source_id=None):
    """Explicit field projection; no reference, labels, identity or score copied."""
    require(transcript_kind in ('CAPTION','VERSIONED_ASR'),'Invalid transcript source')
    if transcript_kind=='VERSIONED_ASR':
        require(record.get('provider')=='faster-whisper' and record.get('profile_id')=='AUDIO_ASR_PROFILE_v0.1' and record.get('ASR_RUN_CONFIG_HASH')==ASR_HASH, 'ASR_PROFILE_PROVENANCE_MISMATCH')
        require(isinstance(record.get('input'),dict) and isinstance(record['input'].get('normalized_wav_sha256'),str), 'MISSING_ASR_AUDIO_BINDING')
    else:
        require(source_id is None or source_id==record.get('video_id'), 'PROJECTED_VIDEO_ID_MISMATCH')
        source_id=record.get('video_id')
    require(isinstance(source_id,str) and bool(source_id), 'MISSING_PROJECTED_SOURCE_ID')
    output=[]
    for key,kind in [('title','TITLE'),('description','DESCRIPTION')]:
        text=record.get(key)
        if isinstance(text,str) and text:
            output.append({'id':key,'kind':kind,'version':version,'source_sha256':source_sha256,
                           'text':text,'asr_profile_hash':None,'source_id':source_id,'asr_input_sha256':None})
    # Stored source caption segments have text/start/duration; only text/time are exposed.
    segments=[]
    for s in record.get('segments',[]):
        if not isinstance(s.get('text'),str): continue
        start=s.get('start');end=s.get('end',start+s.get('duration',0) if isinstance(start,(float,int)) else None)
        if start is None or end is None:continue
        interval(start,end)
        segments.append({'start':start,'end':end,'text':s['text']})
    if segments:
        output.append({'id':'transcript','kind':transcript_kind,'version':version,'source_sha256':source_sha256,
                       'text':json.dumps(segments,ensure_ascii=False),'asr_profile_hash':ASR_HASH if transcript_kind=='VERSIONED_ASR' else None,
                       'source_id':source_id,'asr_input_sha256':record['input']['normalized_wav_sha256'] if transcript_kind=='VERSIONED_ASR' else None})
    return output


def isolated_decision(workspace, binding, bundle, review):
    """OS-constrained reducer: stdin material only, no access to corpus/reference stores."""
    import subprocess
    import tempfile
    root=workspace.root
    # Use the tool's installed interpreter/source; never a production environment.
    from agent_research_intelligence.governance.paths import APP_ROOT
    workspace.checked_path('tmp/context-decision/placeholder',create_parent=True)
    with tempfile.TemporaryDirectory(dir=root/'tmp/context-decision') as d:
        policy='(version 1) (allow default) (deny network*) (deny file-write*)\n'
        policy+='(allow file-write* (literal "/dev/null"))\n'
        for path in [APP_ROOT/'validation',APP_ROOT/'data',APP_ROOT/'docs',APP_ROOT/'secrets',APP_ROOT/'tmp',
                     Path.home()/'.ssh',Path.home()/'.aws',Path.home()/'Library/Keychains']:
            policy+='(deny file-read* (subpath '+json.dumps(str(path))+'))\n'
        profile=Path(d)/'decision.sb';profile.write_text(policy)
        env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(APP_ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1',
             'PYTHONNOUSERSITE':'1','HOME':d,'TMPDIR':d,'LANG':'en_US.UTF-8'}
        payload=canonical({'binding':binding,'bundle':bundle,'review':review})
        require(len(payload)<=2*1024*1024,'Oversized context bundle')
        result=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),str(APP_ROOT/'.venv/bin/python'),'-B',
            str(APP_ROOT/'environments/audio/context_decision_worker.py')],input=payload,capture_output=True,env=env,cwd=APP_ROOT,timeout=30)
        require(result.returncode==0,'ISOLATED_CONTEXT_DECISION_FAILED')
        isolation=json.loads(result.stderr)
        require(isolation=={'reference_stores':'OS_READ_DENIED','input_channel':'STDIN_PROJECTED_MATERIALS_ONLY','network':'OS_DENIED'}, 'CONTEXT_ISOLATION_MISSING')
        decision=json.loads(result.stdout)
        require(decision==decide(binding,bundle,review,created_at=decision.get('created_at')),'CONTEXT_REDUCER_DRIFT')
        workspace.write(f"data/audio/{binding['job_id']}/context/exposure.json", canonical({
            **isolation, 'bundle_sha256':digest(bundle), 'review_sha256':digest(review),
            'material_ids':[m['id'] for m in bundle['materials']],
            'decision_worker_sha256':filehash(Workspace(), 'environments/audio/context_decision_worker.py'),
            'scope':'This decision process only; not a claim that the orchestrator never saw historical scores.'}))
        return decision

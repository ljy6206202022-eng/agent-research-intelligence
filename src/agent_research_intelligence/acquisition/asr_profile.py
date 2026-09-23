"""Fully specified, local-only ASR execution; no quality/authority promotion."""
import dataclasses
import hashlib
import inspect
import json
import logging
import os
import platform
import time
import unicodedata
import warnings
import wave
from importlib import metadata

from agent_research_intelligence.governance.paths import BoundaryError

VERSIONED_PROFILE_ID = 'AUDIO_ASR_PROFILE_v0.1'
VERSIONED_CONFIG_HASH = 'adb335bced2db2bb8b672f5d5be34da4ddf5d03ca830fe77c1bc07b819ffbad5'
ENGLISH_PROFILE_ID = 'AUDIO_ASR_PROFILE_EN_v0.1'
ENGLISH_CONFIG_HASH = '554ef98e3155bb16e280a91da3999ceedd6952e392da247906875b19e7826575'


def load_frozen_profile(workspace):
    profile = json.loads(workspace.read('config/asr/AUDIO_ASR_PROFILE_v0.1.json'))
    validate_profile(profile)
    if (profile.get('id') != VERSIONED_PROFILE_ID
            or profile.get('status') != 'VERSIONED_REPRODUCIBILITY_VERIFIED'
            or profile.get('ASR_RUN_CONFIG_HASH') != VERSIONED_CONFIG_HASH):
        raise BoundaryError('VERSIONED_ASR_PROFILE_DRIFT')
    return profile


def require_profile_language(profile, language):
    if language != profile['config']['transcription']['language']:
        raise BoundaryError('ASR_PROFILE_LANGUAGE_MISMATCH_NEW_PROFILE_REVIEW_REQUIRED')


def load_selected_profile(workspace, profile_id=None):
    """Explicit English opt-in; the frozen Mandarin default remains unchanged."""
    parent = load_frozen_profile(workspace)
    if profile_id in (None, VERSIONED_PROFILE_ID):
        return parent
    if profile_id != ENGLISH_PROFILE_ID:
        raise BoundaryError('UNKNOWN_ASR_PROFILE')
    profile = json.loads(workspace.read('config/asr/' + ENGLISH_PROFILE_ID + '.json'))
    validate_profile(profile)
    expected = json.loads(json.dumps(parent['config']))
    expected['transcription']['language'] = 'en'
    if (profile.get('id') != ENGLISH_PROFILE_ID
            or profile.get('status') != 'USER_APPROVED_ENGLISH_PROFILE'
            or profile.get('parent_config_hash') != VERSIONED_CONFIG_HASH
            or profile.get('ASR_RUN_CONFIG_HASH') != ENGLISH_CONFIG_HASH
            or profile['config'] != expected):
        raise BoundaryError('ENGLISH_ASR_PROFILE_DRIFT')
    return profile


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
                                    ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def file_hash(path):
    result=hashlib.sha256()
    with path.open('rb') as source:
        while block:=source.read(1024**2):result.update(block)
    return result.hexdigest()


def input_identity(path):
    with wave.open(str(path)) as source:
        fields={'sample_rate':source.getframerate(),'channels':source.getnchannels(),
                'bit_depth':source.getsampwidth()*8,'frames':source.getnframes()}
        pcm=source.readframes(source.getnframes())
    return {**fields,'normalized_wav_sha256':file_hash(path),'pcm_frames_sha256':hashlib.sha256(pcm).hexdigest()}


def runtime_identity():
    import faster_whisper,ctranslate2,av,numpy,tokenizers
    from pathlib import Path
    packages={d.metadata['Name'].lower().replace('_','-'):d.version for d in metadata.distributions()}
    binaries={}
    for package in (faster_whisper,ctranslate2,av,numpy,tokenizers):
        base=Path(package.__file__).parent
        digest=hashlib.sha256()
        for path in sorted(p for p in base.rglob('*') if p.suffix in ('.py','.so','.dylib') and p.is_file()):
            digest.update(str(path.relative_to(base)).encode());digest.update(file_hash(path).encode())
        binaries[package.__name__]=digest.hexdigest()
    return {'python':platform.python_version(),'platform':platform.platform(),'machine':platform.machine(),
            'packages':packages,'code_binary_tree_hashes':binaries}


def build_config(workspace):
    from faster_whisper import WhisperModel
    from faster_whisper.feature_extractor import FeatureExtractor
    signature=inspect.signature(WhisperModel.transcribe)
    transcription={name:p.default for name,p in signature.parameters.items() if name not in ('self','audio')}
    transcription.update(language='zh',beam_size=5,word_timestamps=True,vad_filter=False,condition_on_previous_text=False)
    manifest=json.loads(workspace.read('config/permissions/audio-models.json'))
    directory=manifest['asr_directory']
    files=[{k:item[k] for k in ('path','bytes','sha256')} for item in manifest['files'] if item['path'].startswith(directory+'/')]
    env={'OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2','VECLIB_MAXIMUM_THREADS':'2',
         'MKL_NUM_THREADS':'2','TOKENIZERS_PARALLELISM':'false','PYTHONHASHSEED':'0',
         'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
    features={n:p.default for n,p in inspect.signature(FeatureExtractor).parameters.items()}
    return {'runtime':runtime_identity(),'model':{'name':'faster-whisper-small','revision':manifest['asr_revision'],
                'directory':directory,'files':files,'model_hash':canonical_hash(files),'preprocessor_config':'ABSENT_USE_PINNED_LIBRARY_DEFAULTS'},
            'constructor':{'device':'cpu','device_index':0,'compute_type':'int8','cpu_threads':2,'num_workers':1,
                'local_files_only':True,'files':None,'revision':None,'use_auth_token':None,
                'download_root':None,'max_queued_batches':0,'flash_attention':False,'tensor_parallel':False},
            'transcription':transcription,'environment':env,'feature_extractor_defaults':features,
            'instance_policy':'FRESH_PROCESS_AND_FRESH_MODEL_PER_INVOCATION_SERIAL_ONLY',
            'input_mode':'PINNED_PYAV_DECODE_TO_FLOAT32_MONO_16000_NO_EXTRA_TRIM',
            'random_seed':None,'batch_policy':'NON_BATCHED_ONE_AUDIO_ONE_WORKER',
            'silence_trimming':'NONE; VAD disabled; provider no-speech decoder heuristic retained',
            'vad_parameters_effective':'NOT_APPLIED_VAD_DISABLED','effective_chunk_seconds':30,
            'effective_max_tokens':448}


def validate_profile(profile):
    if profile.get('ASR_RUN_CONFIG_HASH')!=canonical_hash(profile['config']):
        raise BoundaryError('ASR_CONFIG_HASH_MISMATCH')
    config=profile['config']
    if config['constructor']['device']!='cpu' or not config['constructor']['local_files_only']:
        raise BoundaryError('ASR_PROFILE_REQUIRES_LOCAL_CPU')
    if config['constructor']['num_workers']!=1 or config['constructor']['cpu_threads']!=2:
        raise BoundaryError('ASR_PROFILE_RESOURCE_DRIFT')
    if config['transcription']['vad_filter'] is not False:
        raise BoundaryError('ASR_REPRO_VAD_MUST_REMAIN_DISABLED')


def transcribe_profile(workspace,path,profile,*,expected_input=None):
    validate_profile(profile);config=profile['config']
    if runtime_identity()!=config['runtime']:raise BoundaryError('ASR_RUNTIME_DRIFT')
    for name,value in config['environment'].items():
        if os.environ.get(name)!=value:raise BoundaryError('ASR_ENVIRONMENT_DRIFT_'+name)
    model=config['model'];directory=workspace.checked_path(model['files'][0]['path']).parent
    if str(directory.relative_to(workspace.root))!=model['directory']:
        raise BoundaryError('ASR_MODEL_DIRECTORY_DRIFT')
    if {p.name for p in directory.iterdir() if p.is_file() and not p.name.endswith('.receipt.json')}!={p['path'].split('/')[-1] for p in model['files']}:
        raise BoundaryError('ASR_MODEL_INVENTORY_DRIFT')
    for item in model['files']:
        source=workspace.checked_path(item['path'])
        if source.stat().st_size!=item['bytes'] or file_hash(source)!=item['sha256']:
            raise BoundaryError('ASR_MODEL_HASH_DRIFT')
    before=input_identity(path)
    if expected_input and any(before[k]!=expected_input[k] for k in before):
        raise BoundaryError('AUDIO_NORMALIZATION_NONDETERMINISM')
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    parameters={n for n in inspect.signature(WhisperModel.transcribe).parameters if n not in ('self','audio')}
    if set(config['transcription'])!=parameters:raise BoundaryError('ASR_DECODER_PARAMETER_DRIFT')
    if config['random_seed'] is not None:
        import ctranslate2
        ctranslate2.set_random_seed(config['random_seed'])
    events=[]
    class Capture(logging.Handler):
        def emit(self,record):
            message=record.getMessage()
            if len(events)<400 and (record.levelno>=logging.WARNING or 'threshold' in message.lower()):
                events.append({'level':record.levelname,'message':message[:1000]})
    logger=logging.getLogger('faster_whisper');handler=Capture();previous_level=logger.level
    logger.addHandler(handler);logger.setLevel(logging.DEBUG)
    started=time.monotonic()
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always')
            model_instance=WhisperModel(str(directory),**config['constructor'])
            waveform=decode_audio(str(path),sampling_rate=16000)
            array_hash=hashlib.sha256(waveform.tobytes()).hexdigest()
            segments,info=model_instance.transcribe(waveform,**config['transcription'])
            output=[dataclasses.asdict(segment) for segment in segments]
            python_warnings=[{'category':w.category.__name__,'message':str(w.message)[:1000]} for w in captured]
    finally:
        logger.removeHandler(handler);logger.setLevel(previous_level)
    if input_identity(path)!=before:raise BoundaryError('ASR_INPUT_CHANGED_DURING_RUN')
    text=''.join(segment['text'] for segment in output)
    # Exact raw text is also compared; normalization only NFC plus whitespace.
    normalized=' '.join(unicodedata.normalize('NFC',text).split())
    words=[w for s in output for w in s['words'] or []]
    result={'provider':'faster-whisper','profile_id':profile['id'],'ASR_RUN_CONFIG_HASH':profile['ASR_RUN_CONFIG_HASH'],
        'model_hash':config['model']['model_hash'],'input':before,'model_input_float32_sha256':array_hash,
        'runtime_s':round(time.monotonic()-started,6),'language':info.language,'language_probability':info.language_probability,
        'detected_language':info.language if config['transcription']['language'] is None else None,
        'language_mode':'FORCED' if config['transcription']['language'] else 'DETECTED',
        'language_probability_is_detection':config['transcription']['language'] is None,
        'text':text,'normalized_text':normalized,'segments':output,'number_of_segments':len(output),
        'number_of_words':len(words),'start_s':output[0]['start'] if output else None,'end_s':output[-1]['end'] if output else None,
        'effective_transcription_options':dataclasses.asdict(info.transcription_options),
        'duration':info.duration,'duration_after_vad':info.duration_after_vad,
        'decoding_events':events,'warnings':python_warnings,'quality':'UNASSESSED',
        'text_hash':canonical_hash(text),'normalized_text_hash':canonical_hash(normalized),
        'segments_hash':canonical_hash(output),'word_structure_hash':canonical_hash(words)}
    return result

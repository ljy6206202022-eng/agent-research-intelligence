"""Explicit audio entry point in the separate audio environment; no provider auto-install."""
import argparse
import json
from pathlib import Path
import sys

# Only our own package, never Project or Agent runtime paths.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from agent_research_intelligence.governance.paths import Workspace, BoundaryError
from agent_research_intelligence.acquisition.audio_pipeline import run_audio

parser = argparse.ArgumentParser(description='audio local audio validation only')
source = parser.add_mutually_exclusive_group(required=True)
source.add_argument('--video')
source.add_argument('--replay-manifest',help='Explicit sealed native Parakeet output replay, no model/audio claim')
source.add_argument('--local-manifest',help='Owned workspace-relative prepared PCM provenance JSON')
parser.add_argument('--purpose',choices=['NO_SUBTITLE','MULTIPLE_SPEAKER'])
parser.add_argument('--offset',type=float,default=0)
parser.add_argument('--duration',type=float)
parser.add_argument('--speakers',type=int,default=-1)
parser.add_argument('--language',choices=['en','zh'])
parser.add_argument('--acquisition-basis')
parser.add_argument('--diarization-provider',choices=['sherpa-onnx','community1'],default=None)
parser.add_argument('--asr-profile', choices=['AUDIO_ASR_PROFILE_v0.1','AUDIO_ASR_PROFILE_EN_v0.1','AUDIO_ASR_PROFILE_EN_TECH_PARAKEET_v0.1'])
parser.add_argument('--reuse-diarization-manifest', help='Sealed same-input Community-1 raw segments; no diarization inference')
parser.add_argument('--caption-test-withhold',action='store_true',help='Testing only: intentionally withhold existing captions')
parser.add_argument('--speaker-alignment',choices=['ordinary','exclusive'],default='ordinary')
args = parser.parse_args()
legacy_provider = args.diarization_provider or 'sherpa-onnx'
try:
    if args.asr_profile == 'AUDIO_ASR_PROFILE_EN_TECH_PARAKEET_v0.1':
        if args.video or args.speakers!=-1 or args.language not in (None,'en') or args.offset!=0 or args.duration is not None or args.purpose is not None or args.acquisition_basis is not None:
            raise BoundaryError('PARAKEET_REQUIRES_EXPLICIT_PREPARED_OR_REPLAY_INPUT')
        from agent_research_intelligence.acquisition.parakeet_audio import run_parakeet_audio
        ws=Workspace()
        result=run_parakeet_audio(ws,
            source=json.loads(ws.read(args.local_manifest)) if args.local_manifest else None,
            replay=json.loads(ws.read(args.replay_manifest)) if args.replay_manifest else None,
            withhold_captions=args.caption_test_withhold,diarization_provider=args.diarization_provider,
            diarization_reuse=json.loads(ws.read(args.reuse_diarization_manifest)) if args.reuse_diarization_manifest else None,
            alignment=args.speaker_alignment)
    elif args.replay_manifest or args.caption_test_withhold or args.speaker_alignment!='ordinary':
        raise BoundaryError('PARAKEET_ONLY_OPTIONS_REQUIRE_EXPLICIT_PROFILE')
    elif args.reuse_diarization_manifest:
        if (not args.local_manifest or args.asr_profile != 'AUDIO_ASR_PROFILE_EN_v0.1'
                or args.diarization_provider != 'community1' or args.speakers != -1):
            raise BoundaryError('REUSED_DIARIZATION_REQUIRES_EXPLICIT_ENGLISH_LOCAL_INPUT')
        from agent_research_intelligence.acquisition.joint_audio import run_prepared_joint
        ws=Workspace()
        result=run_prepared_joint(ws,source=json.loads(ws.read(args.local_manifest)),
            profile_id=args.asr_profile,reuse=json.loads(ws.read(args.reuse_diarization_manifest)))
    elif args.asr_profile is not None:
        raise BoundaryError('EXPLICIT_ASR_PROFILE_REQUIRES_JOINT_PREPARED_INPUT')
    elif args.local_manifest:
        if args.speakers != -1:
            raise BoundaryError('Bare speaker count denied')
        from agent_research_intelligence.acquisition.prepared_audio import run_prepared_audio
        ws=Workspace()
        result=run_prepared_audio(ws,source=json.loads(ws.read(args.local_manifest)),
                                  diarization_provider=legacy_provider)
    else:
        if args.purpose is None or args.duration is None or args.acquisition_basis is None:
            raise BoundaryError('Video requires purpose, duration and acquisition basis')
        result = run_audio(Workspace(),url=args.video,purpose=args.purpose,offset_s=args.offset,duration_s=args.duration,
                           speakers=args.speakers,language=args.language,acquisition_basis=args.acquisition_basis,
                           diarization_provider=legacy_provider)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['state'] in ('EVIDENCE_READY_QUALITY_UNASSESSED','CAPTIONS_PREFERRED') else 2)
except (BoundaryError,OSError,ValueError) as exc:
    print(json.dumps({'status':'STOP','exception_type':type(exc).__name__})); raise SystemExit(2)

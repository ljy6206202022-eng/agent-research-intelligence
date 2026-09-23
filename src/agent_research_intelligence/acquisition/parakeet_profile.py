"""Explicit immutable Parakeet configuration, no downloads or default mutation."""
import json
from importlib.metadata import version
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.acquisition.asr_profile import file_hash
PROFILE_ID = 'AUDIO_ASR_PROFILE_EN_TECH_PARAKEET_v0.1'
def load_profile(ws, *, assets=False, runtime=False):
 path=ws.checked_path('config/asr/'+PROFILE_ID+'.json')
 p=json.loads(path.read_text())
 if p.get('id')!=PROFILE_ID or p.get('status')!='USER_CONFIGURED_LOCAL_ONLY':raise BoundaryError('PARAKEET_PROFILE_NOT_CONFIGURED')
 if not p.get('model',{}).get('files'):raise BoundaryError('PARAKEET_MODEL_MANIFEST_REQUIRED')
 if not p['model'].get('directory','').startswith('cache/models/audio/'):
  raise BoundaryError('PARAKEET_MODEL_DIRECTORY_DENIED')
 if any(not f.get('path','').startswith(p['model']['directory']+'/') for f in p['model']['files']):
  raise BoundaryError('PARAKEET_MODEL_PATH_DENIED')
 if p.get('lock') and file_hash(ws.checked_path(p['lock']))!=p.get('lock_sha256'):raise BoundaryError('PARAKEET_LOCK_DRIFT')
 if assets:
  for f in p['model']['files']:
   path=ws.checked_path(f['path'])
   if path.stat().st_size!=f['bytes'] or file_hash(path)!=f['sha256']:raise BoundaryError('PARAKEET_MODEL_DRIFT')
 if runtime:
  if any(version(k)!=v for k,v in p['runtime_versions'].items()):raise BoundaryError('PARAKEET_VERSION_DRIFT')
  if any(file_hash(ws.checked_path(k))!=v for k,v in p.get('runtime_files',{}).items()):raise BoundaryError('PARAKEET_RUNTIME_DRIFT')
 p['profile_sha256']=file_hash(ws.checked_path('config/asr/'+PROFILE_ID+'.json'))
 return p

"""Owned process groups, one heavy slot, bounded resources and temporary media."""
from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import signal
import stat
import subprocess
import time

from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.connectors.public_http import AcquisitionError

GIB = 1024**3


def tree_bytes(path):
    total = 0
    if not path.exists(): return total
    for base, directories, files in os.walk(path, followlinks=False):
        directories[:] = [d for d in directories if not (Path(base)/d).is_symlink()]
        for name in files:
            try:
                info = (Path(base)/name).lstat()
                if stat.S_ISREG(info.st_mode): total += info.st_size
            except FileNotFoundError:
                continue
    return total


class Budget:
    def __init__(self, workspace, psutil, *, on_memory_stop=None):
        self.workspace, self.psutil = workspace, psutil
        self.on_memory_stop = on_memory_stop
        self.last_disk_check = -float('inf')

    def check(self, *, force=False):
        available = self.psutil.virtual_memory().available
        if available < 6*GIB:
            if self.on_memory_stop is not None:
                try:
                    self.on_memory_stop('SYSTEM_MEMORY_LOW_PAUSED', available)
                except Exception:
                    # A diagnostic write cannot weaken or replace the guard.
                    pass
            raise AcquisitionError('SYSTEM_MEMORY_LOW_PAUSED')
        if self.psutil.disk_usage(str(self.workspace.root)).free < 20*GIB:
            raise AcquisitionError('DISK_FREE_LOW_PAUSED')
        if force or time.monotonic()-self.last_disk_check > 5:
            for folder, maximum in [('',30*GIB), ('cache/models',8*GIB), ('tmp/media',2*GIB), ('data/artifacts',8*GIB), ('logs',512*1024**2)]:
                if tree_bytes(self.workspace.root/folder) >= maximum:
                    raise AcquisitionError('QUOTA_EXCEEDED_' + (folder.replace('/','_') or 'REPOSITORY'))
            self.last_disk_check = time.monotonic()


@contextmanager
def heavy_slot(workspace):
    with workspace.parent_fd('tmp/audio-heavy.lock', create=True) as (parent, name):
        fd = os.open(name, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise BoundaryError('Unsafe lock file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AcquisitionError('HEAVY_SLOT_BUSY') from exc
            yield
        finally:
            os.close(fd)


def clean_media(workspace, job_id):
    if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise BoundaryError('Invalid job ID')
    removed = []
    names=['source.audio','source.video','normalized.wav']
    directory=workspace.root/'tmp/media'/job_id
    if directory.exists():
        names.extend(p.name for p in directory.iterdir() if re.fullmatch(
            r'(?:source\.(?:audio|video)(?:\.part)?(?:-Frag\d+|\.ytdl|\.ytdl\.tmp)|candidate-\d+\.png)',p.name))
    for name in names:
        rel = f'tmp/media/{job_id}/{name}'
        try:
            workspace.checked_path(rel)
            with workspace.parent_fd(rel) as (parent, leaf):
                try:
                    os.unlink(leaf, dir_fd=parent)
                    removed.append(rel)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    return removed


def sanitized_env(workspace, job_id):
    root = str(workspace.root)
    # Whitelist, rather than copy os.environ: no tokens, proxies, user caches.
    return {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
            'PYTHONPATH': root+'/src', 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONNOUSERSITE': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
            'HF_HOME': root+'/cache/models/hf', 'XDG_CACHE_HOME': root+'/cache/audio',
            'TMPDIR': root+'/tmp/media/'+job_id,
            'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2', 'VECLIB_MAXIMUM_THREADS': '2',
            'MKL_NUM_THREADS': '2', 'TOKENIZERS_PARALLELISM': 'false',
            'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1'}


def reap_orphaned_media(workspace, psutil):
    """Explicit-startup cleanup, not a background Watch. Never kill stale PIDs."""
    removed=[]
    directory=workspace.root/'tmp/media'
    if not directory.exists(): return removed
    for marker in directory.glob('*/owned.json'):
        owner=json.loads(workspace.read(str(marker.relative_to(workspace.root))))
        job_id=marker.parent.name
        if owner.get('job_id')!=job_id or owner.get('purpose')!='AUDIO_TEMP_MEDIA':
            raise BoundaryError('Unrecognized media ownership marker')
        if 'supervisor_pid' not in owner: continue
        try:
            if abs(psutil.Process(owner['supervisor_pid']).create_time()-owner['supervisor_created'])<.01: continue
        except psutil.NoSuchProcess: pass
        active=False
        for stage in ('media','normalize','asr','diarize'):
            rel=f'data/audio/{job_id}/{stage}-process.json'
            path=workspace.checked_path(rel,create_parent=True)
            if not path.exists(): continue
            record=json.loads(workspace.read(rel))
            try:
                proc=psutil.Process(record['pid'])
                if record.get('created') is None or abs(proc.create_time()-record['created'])<.01: active=True
            except psutil.NoSuchProcess: pass
        if not active: removed.extend(clean_media(workspace,job_id))
    return removed


def worker_sandbox_profile(workspace, job_id, stage, provider, *, isolate_references=False):
    root = workspace.root
    input_rel = f'data/audio/{job_id}/job.json'
    job_dir = str(root/'tmp/media'/job_id)
    # Native macOS worker confinement, additional to application path checks.
    profile = '(version 1) (allow default) (deny network*) (deny file-write*)\n' + \
        '(allow file-write* (subpath '+json.dumps(job_dir)+') (literal "/dev/null"))\n'
    for private in ('.ssh', '.aws', '.config', 'Library/Keychains'):
        profile += '(deny file-read* (subpath '+json.dumps(str(Path.home()/private))+'))\n'
    if stage == 'media':
        # Seatbelt accepts "localhost", not numeric hosts, in this filter.
        # Application transport still connects only to IPv4 127.0.0.1:9674.
        profile += '(allow network-outbound (remote tcp "localhost:9674"))\n'
    if (provider == 'community1' and stage in ('asr', 'diarize')) or isolate_references:
        # Predictors see only their own job and temporary audio, never reference answers.
        for path in (root/'secrets', root/'validation', root/'docs',
                     Path.home()/'.cache/huggingface', Path.home()/'.huggingface',
                     Path.home()/'Library/Keychains'):
            profile += '(deny file-read* (subpath '+json.dumps(str(path))+'))\n'
        allowed = ' '.join('(literal '+json.dumps(str(p))+')' for p in (root/'data', root/'data/audio', root/'data/audio'/job_id, root/input_rel))
        profile += '(deny file-read* (require-all (subpath '+json.dumps(str(root/'data'))+') (require-not (require-any '+allowed+'))))\n'
    return profile


def run_worker(workspace, job_id, stage, budget, *, timeout_s=3600):
    if stage not in ('media','normalize','asr','diarize','frames'):
        raise BoundaryError('Worker stage denied')
    root = workspace.root
    input_rel = f'data/audio/{job_id}/job.json'
    workspace.checked_path(input_rel)
    from agent_research_intelligence.acquisition.community1 import selected, manifest
    job = json.loads(workspace.read(input_rel)) if stage in ('diarize', 'asr') else {}
    provider = selected(job)
    job_dir = str(root/'tmp/media'/job_id)
    profile = worker_sandbox_profile(workspace, job_id, stage, provider,
        isolate_references=stage == 'asr' and job.get('asr_profile_id') == 'AUDIO_ASR_PROFILE_EN_v0.1')
    workspace.write(f'tmp/media/{job_id}/worker.sb', profile.encode(), replace=True)
    interpreter = root/'environments/audio/.venv/bin/python'
    script = root/'environments/audio/worker.py'
    if stage=='frames':
        script=root/'environments/audio/frame_worker.py'
    if stage == 'asr' and job.get('asr_profile_id') == 'AUDIO_ASR_PROFILE_EN_v0.1':
        script = root/'environments/audio/english_worker.py'
    if stage == 'diarize':
        if provider == 'community1':
            manifest(workspace)
            interpreter = root/'environments/pyannote_candidate/.venv/bin/python'
            script = root/'environments/pyannote_candidate/integration_worker.py'
        else:
            from agent_research_intelligence.acquisition.speaker_context import validate_job
            validate_job(workspace, job)
            script = root/'environments/audio/context_worker.py'
    args = ['/usr/bin/sandbox-exec','-f',str(root/f'tmp/media/{job_id}/worker.sb'),str(interpreter),'-B',str(script),stage,input_rel]
    budget.check(force=True)
    available_before = budget.psutil.virtual_memory().available
    if provider == 'community1' and available_before < 12*GIB:
        raise AcquisitionError('AUDIO_INTEGRATION_START_RESOURCE_WAIT_12GIB')
    started = time.monotonic(); peak = 0; warned = False
    worker_env = sanitized_env(workspace, job_id)
    if provider == 'community1':
        worker_env.update(HF_HOME=job_dir+'/hf', HF_TOKEN_PATH=job_dir+'/no-token',
                          XDG_CACHE_HOME=job_dir+'/cache', XDG_CONFIG_HOME=job_dir+'/config',
                          MPLCONFIGDIR=job_dir+'/mpl', PYANNOTE_METRICS_ENABLED='0')
    if stage == 'asr':
        worker_env['PYTHONHASHSEED'] = '0'
    with workspace.parent_fd(f'tmp/media/{job_id}/{stage}.log',create=True) as (parent,name):
        fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'wb') as log:
            proc = subprocess.Popen(args,cwd=root,env=worker_env,
                                    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try: created=budget.psutil.Process(proc.pid).create_time()
            except budget.psutil.NoSuchProcess: created=None
            workspace.write(f'data/audio/{job_id}/{stage}-process.json',json.dumps({'pid':proc.pid,'created':created,'stage':stage,'owned_process_group':True}).encode())
            try:
                while proc.poll() is None:
                    budget.check()
                    try:
                        parent_proc = budget.psutil.Process(proc.pid)
                        members = [parent_proc, *parent_proc.children(recursive=True)]
                    except budget.psutil.NoSuchProcess:
                        if proc.poll() is not None: break
                        raise AcquisitionError('WORKER_PROCESS_UNOBSERVABLE')
                    rss = 0
                    for member in members:
                        try: rss += member.memory_info().rss
                        except budget.psutil.NoSuchProcess: pass
                    # Include this tool's supervisor in the measured total.
                    rss += budget.psutil.Process(os.getpid()).memory_info().rss
                    peak = max(peak,rss); warned |= rss >= 4*GIB
                    if rss >= 6*GIB: raise AcquisitionError('WORKER_RSS_LIMIT')
                    if log.tell() >= 8*1024**2 or os.fstat(log.fileno()).st_size >= 8*1024**2:
                        raise AcquisitionError('WORKER_LOG_LIMIT')
                    if time.monotonic()-started > timeout_s: raise AcquisitionError('WORKER_DEADLINE')
                    time.sleep(.5)
                if proc.returncode:
                    raise AcquisitionError('WORKER_FAILED_'+stage.upper())
            except BaseException as exc:
                # Only the group created immediately above; never name-based killing.
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait(timeout=10)
                workspace.write(f'data/audio/{job_id}/{stage}-resources.json',json.dumps({
                    'stage':stage,'status':'FAILED','error':getattr(exc,'code',type(exc).__name__),
                    'peak_tool_rss_bytes':peak,'elapsed_s':round(time.monotonic()-started,3),
                    'owned_group_stopped':True}).encode())
                raise
    receipt = {'stage':stage,'status':'PASS','elapsed_s':round(time.monotonic()-started,3),
               'peak_tool_rss_bytes':peak,'rss_warning':warned,'network':'APPROVED_PROXY_ONLY' if stage=='media' else 'DENIED_BY_SANDBOX',
               'cpu_threads':2,'priority':'nice_10', 'diarization_provider':provider,
               'available_memory_before_bytes':available_before}
    workspace.write(f'data/audio/{job_id}/{stage}-resources.json',json.dumps(receipt,indent=2).encode())
    return receipt

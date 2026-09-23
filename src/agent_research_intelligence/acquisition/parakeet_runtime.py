"""Existing resource limits applied to the fixed local Parakeet worker."""
import ctypes,json,os,signal,subprocess,time
from pathlib import Path
from agent_research_intelligence.acquisition.audio_runtime import worker_sandbox_profile,sanitized_env,GIB
from agent_research_intelligence.acquisition.asr_profile import file_hash
from agent_research_intelligence.governance.paths import BoundaryError
class Usage(ctypes.Structure):
    _fields_=[('uuid',ctypes.c_uint8*16)]+[(n,ctypes.c_uint64) for n in ['user_time','system_time','pkg_idle_wkups','interrupt_wkups','pageins','wired_size','resident_size','phys_footprint','proc_start_abstime','proc_exit_abstime']]
def physical_footprint(pid):
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True);u=Usage()
    if lib.proc_pid_rusage(pid,0,ctypes.byref(u))!=0:raise OSError(ctypes.get_errno(),'proc_pid_rusage')
    return u.phys_footprint

def run_parakeet_worker(ws,job_id,budget):
    import psutil,imageio_ffmpeg
    temp=ws.root/'tmp/media'/job_id;prefix='data/audio/'+job_id
    ff=Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not ff.resolve().is_relative_to(ws.root/'environments/audio/.venv'):raise BoundaryError('GLOBAL_FFMPEG_DENIED')
    (temp/'bin').mkdir();(temp/'bin/ffmpeg').symlink_to(ff)
    sb=worker_sandbox_profile(ws,job_id,'asr','sherpa-onnx',isolate_references=True)
    (temp/'parakeet.sb').write_text(sb)
    env=sanitized_env(ws,job_id);env.update(PATH=str(temp/'bin')+':/usr/bin:/bin',HF_HOME=str(temp/'hf'),HF_TOKEN_PATH=str(temp/'no-token'),HF_HUB_DISABLE_IMPLICIT_TOKEN='1',HF_HUB_DISABLE_XET='1',XDG_CACHE_HOME=str(temp/'cache'),XDG_CONFIG_HOME=str(temp/'config'),NUMBA_CACHE_DIR=str(temp/'numba'),MPLCONFIGDIR=str(temp/'mpl'),PYTHONHASHSEED='0')
    worker=ws.root/'environments/audio/parakeet_worker.py';args=['/usr/bin/sandbox-exec','-f',str(temp/'parakeet.sb'),str(ws.root/'environments/parakeet_candidate/.venv/bin/python'),'-B',str(worker),'asr',prefix+'/job.json']
    ws.write(prefix+'/prediction-seal.json',json.dumps({'worker_sha256':file_hash(worker),'job_sha256':file_hash(ws.root/prefix/'job.json'),'ffmpeg_sha256':file_hash(ff),'profile_sha256':file_hash(ws.root/'config/asr/AUDIO_ASR_PROFILE_EN_TECH_PARAKEET_v0.1.json'),'runtime_sha256':file_hash(Path(__file__)),'sandbox_sha256':file_hash(temp/'parakeet.sb'),'reference_visible':False,'args':args}).encode())
    budget.check(force=True);start=time.monotonic();peak=footpeak=0;minimum=psutil.virtual_memory().available;swap=psutil.swap_memory().used;code=None
    try:
        with (ws.root/prefix/'worker.log').open('xb') as log,(ws.root/prefix/'system-metrics.jsonl').open('x') as metrics:
            proc=subprocess.Popen(args,cwd=ws.root,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
            ws.write(prefix+'/asr-process.json',json.dumps({'pid':proc.pid,'created':psutil.Process(proc.pid).create_time(),'stage':'asr','owned_process_group':True}).encode())
            try:
                while proc.poll() is None:
                    budget.check();minimum=min(minimum,psutil.virtual_memory().available)
                    try:members=[psutil.Process(proc.pid),*psutil.Process(proc.pid).children(recursive=True),psutil.Process()]
                    except psutil.NoSuchProcess:
                        if proc.poll() is not None:break
                        raise
                    rss=foot=0
                    for member in members:
                        try:rss+=member.memory_info().rss;foot+=physical_footprint(member.pid)
                        except psutil.NoSuchProcess:pass
                        except OSError:
                            if member.is_running():raise
                    peak=max(peak,rss);footpeak=max(footpeak,foot)
                    metrics.write(json.dumps({'elapsed_s':time.monotonic()-start,'rss':rss,'physical_footprint':foot,'available':psutil.virtual_memory().available,'swap':psutil.swap_memory().used})+'\n');metrics.flush()
                    if max(rss,foot)>=6*GIB:raise BoundaryError('PARAKEET_TASK_MEMORY_LIMIT')
                    if time.monotonic()-start>3600:raise BoundaryError('PARAKEET_DEADLINE')
                    if os.fstat(log.fileno()).st_size>=8*1024**2:raise BoundaryError('PARAKEET_LOG_LIMIT')
                    time.sleep(.5)
                code=proc.returncode
                if code:raise BoundaryError('PARAKEET_WORKER_FAILED')
            except BaseException:
                if proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL)
                proc.wait(timeout=10);code=proc.returncode;raise
    finally:
        ws.write(prefix+'/resources.json',json.dumps({'exit_code':code,'elapsed_s':time.monotonic()-start,'peak_task_rss_bytes':peak,'peak_task_physical_footprint_bytes':footpeak,'minimum_system_available_bytes':minimum,'swap_before_bytes':swap,'swap_after_bytes':psutil.swap_memory().used,'metrics_overlap_do_not_sum':True,'network':'OS_DENIED','reference':'OS_DENIED'}).encode())
        for name in ['raw-api.json','raw-cli.json','ASR_RAW.txt','execution.json','effective-config.json','device.json','isolation.json','mlx-metrics.jsonl','chunk-progress.jsonl','resource-violation.json','telemetry-failure.json']:
            if (temp/name).is_file():ws.write(prefix+'/'+name,(temp/name).read_bytes())

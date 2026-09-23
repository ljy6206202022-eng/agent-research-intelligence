"""Bounded Parakeet API experiment; no decoding, chunking or alignment reimplementation."""
from pathlib import Path
import ctypes,dataclasses,hashlib,json,os,signal,socket,sys,threading,time,traceback
import psutil
ROOT=Path(__file__).resolve().parents[2]
GIB=1024**3
class Usage(ctypes.Structure):
    _fields_=[('uuid',ctypes.c_uint8*16)]+[(n,ctypes.c_uint64) for n in ['user_time','system_time','pkg_idle_wkups','interrupt_wkups','pageins','wired_size','resident_size','phys_footprint','proc_start_abstime','proc_exit_abstime']]
def physical_footprint(pid):
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True);u=Usage()
    if lib.proc_pid_rusage(pid,0,ctypes.byref(u))!=0:raise OSError(ctypes.get_errno(),'proc_pid_rusage')
    return u.phys_footprint

def save(path,value):
    with path.open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2,default=str,allow_nan=False)
def main():
    stage,job_name=sys.argv[1:];job=json.loads((ROOT/job_name).read_text());temp=ROOT/'tmp/media'/job['job_id']
    assert job_name==f'data/audio/{job["job_id"]}/job.json' and job['purpose']=='PARAKEET_LIMITED_AUDIO'
    os.nice(10)
    supervisor=os.getppid();stop=threading.Event()
    def orphan_guard():
        while not stop.wait(1):
            if os.getppid()!=supervisor:os.killpg(os.getpgrp(),signal.SIGKILL)
    threading.Thread(target=orphan_guard,daemon=True).start()
    from agent_research_intelligence.governance.paths import Workspace
    from agent_research_intelligence.acquisition.parakeet_profile import load_profile
    from agent_research_intelligence.acquisition.asr_profile import input_identity
    profile=load_profile(Workspace(),assets=True,runtime=True)
    assert job['asr_profile_id']==profile['id'] and job['model_directory']==profile['model']['directory']
    assert input_identity(temp/'normalized.wav')==job['expected_input']
    denied={}
    for name,path in [('reference',ROOT/'validation'),('credentials',ROOT/'secrets')]:
        try:list(path.iterdir());denied[name]=False
        except PermissionError:denied[name]=True
    try:
        with socket.socket() as connection:connection.settimeout(1);connection.connect(('127.0.0.1',9))
        denied['network']=False
    except PermissionError:denied['network']=True
    assert all(denied.values()), 'PREDICTOR_ISOLATION_REQUIRED'
    save(temp/'isolation.json',denied)
    assert stage=='asr'
    import mlx.core as mx
    mx.set_default_device(mx.gpu)
    mx.set_memory_limit(6*GIB);mx.set_cache_limit(256*1024**2);mx.reset_peak_memory()
    save(temp/'device.json',{'device':mx.device_info(),'default_device':str(mx.default_device()),'mlx_memory_limit':6*GIB,'mlx_cache_limit':256*1024**2,'resource_controls_only':True})
    def telemetry():
        try:
            with (temp/'mlx-metrics.jsonl').open('x') as f:
                while not stop.is_set():
                    p=psutil.Process();children=p.children(recursive=True);rss=p.memory_info().rss;foot=physical_footprint(p.pid)
                    for c in children:
                        try:rss+=c.memory_info().rss;foot+=physical_footprint(c.pid)
                        except (psutil.NoSuchProcess,OSError):pass
                    active=mx.get_active_memory();cache=mx.get_cache_memory();peak=mx.get_peak_memory()
                    row={'monotonic':time.monotonic(),'rss_bytes':rss,'physical_footprint_bytes':foot,'mlx_active_bytes':active,'mlx_cache_bytes':cache,'mlx_peak_active_bytes':peak,'system_available_bytes':psutil.virtual_memory().available,'swap_used_bytes':psutil.swap_memory().used,'threads':p.num_threads()}
                    f.write(json.dumps(row)+'\n');f.flush()
                    if max(rss,foot,active+cache,peak)>=6*GIB or row['system_available_bytes']<6*GIB:
                        save(temp/'resource-violation.json',row);os.killpg(os.getpgrp(),signal.SIGKILL)
                    stop.wait(.25)
        except BaseException as e:
            try:save(temp/'telemetry-failure.json',{'type':type(e).__name__,'error':str(e)})
            finally:os.killpg(os.getpgrp(),signal.SIGKILL)
    thread=threading.Thread(target=telemetry,daemon=True);thread.start()
    from parakeet_mlx import from_pretrained
    from parakeet_mlx.cli import to_json,to_txt
    from parakeet_mlx.parakeet import DecodingConfig
    begin=time.monotonic();model=from_pretrained(str(ROOT/job['model_directory']))
    mx.eval(model.parameters());loaded=time.monotonic()
    assert model.preprocessor_config.sample_rate==16000
    effective={'decoding':dataclasses.asdict(DecodingConfig()),'preprocessor':dataclasses.asdict(model.preprocessor_config),'dtype':'bfloat16 (official default)','chunk_duration':120.0,'overlap_duration':15.0,'language':'automatic multilingual; API has no language/prompt option','from_pretrained_local_path':job['model_directory']}
    assert effective==profile['configuration'], 'PARAKEET_EFFECTIVE_CONFIG_DRIFT'
    save(temp/'effective-config.json',effective)
    def progress(end,total):
        # Upstream invokes this before processing each chunk, not after completion.
        with (temp/'chunk-progress.jsonl').open('a') as f:f.write(json.dumps({'at':time.monotonic(),'next_chunk_end_sample':end,'total_samples':total,'event':'UPSTREAM_BEFORE_CHUNK'})+'\n')
    result=model.transcribe(str(temp/'normalized.wav'),chunk_duration=120.0,overlap_duration=15.0,chunk_callback=progress)
    mx.synchronize();finished=time.monotonic()
    with (temp/'raw-api.json').open('x') as f:json.dump(dataclasses.asdict(result),f,ensure_ascii=False,indent=2,allow_nan=False)
    (temp/'raw-cli.json').open('x').write(to_json(result));(temp/'ASR_RAW.txt').open('x').write(to_txt(result)+'\n')
    save(temp/'execution.json',{'status':'VALID_PREDICTION_COMPLETE','valid_predictions':1,'scope':'NORMAL_ENTRY_INTEGRATION_OR_USER_JOB','model_loads':1,'load_s':loaded-begin,'transcribe_s':finished-loaded,'load_plus_transcribe_s':finished-begin,'mlx_peak_active_bytes':mx.get_peak_memory(),'mlx_final_active_bytes':mx.get_active_memory(),'mlx_final_cache_bytes':mx.get_cache_memory(),'raw_text_sha256':hashlib.sha256(result.text.encode()).hexdigest(),'HUMAN_VERIFIED':False,'Community1_runs':0})
    stop.set();thread.join(timeout=3)

if __name__=='__main__':
    try:main()
    except BaseException as exc:
        print(json.dumps({'exception_type':type(exc).__name__,'error':str(exc)[:2000]}),flush=True);traceback.print_exc();raise

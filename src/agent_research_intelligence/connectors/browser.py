"""Dedicated Chromium, OS-denied network, bounded public HTTPS broker over stdio.

No daily browser, cookies, CDP port, arbitrary scripts, forms or account actions.
The browser never receives private context. Page content remains untrusted.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from datetime import datetime,timezone,timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from agent_research_intelligence.governance.paths import BoundaryError, Workspace
from agent_research_intelligence.connectors.public_http import PublicHTTP, AcquisitionError
from agent_research_intelligence.acquisition.audio_runtime import Budget, heavy_slot, GIB


def check_browser_request(initial, request, count):
    if count>30:raise AcquisitionError('BROWSER_REQUEST_LIMIT')
    if request['method']!='GET' or request['resource_type'] not in ('document','script','stylesheet','image','font'):
        raise AcquisitionError('BROWSER_ACTION_DENIED')
    if urlsplit(request['url']).hostname!=urlsplit(initial).hostname:
        raise AcquisitionError('BROWSER_CROSS_ORIGIN_DENIED')


class Browser:
    def __init__(self, workspace, http=None):
        self.ws=workspace;self.http=http or PublicHTTP(timeout=8,max_bytes=2*1024**2)

    def fetch(self,url):
        import psutil
        self.http._destination(url)
        permission=json.loads(self.ws.read('config/permissions/discovery-browser.json'))
        if permission.get('dedicated_browser') is not True or permission.get('authority')!='USER_DISCOVERY_AUTHORIZATION':
            raise BoundaryError('Dedicated browser not authorized')
        with heavy_slot(self.ws):
            budget=Budget(self.ws,psutil);budget.check(force=True)
            job=uuid4().hex;rel='tmp/browser/'+job;root=self.ws.root
            self.ws.write(rel+'/request.json',json.dumps({'url':url}).encode())
            directory=root/rel
            # Reuse the existing worker confinement: no network, job-only writes,
            # deny production, credential and daily-browser stores explicitly.
            profile='(version 1) (allow default) (deny network*) (deny file-write*)\n'
            runtime=str((root/'environments/research/.venv/bin/python').resolve().parents[1])
            profile+='(deny file-read-data (require-all (subpath '+json.dumps(str(Path.home()))+') (require-not (require-any (subpath '+json.dumps(str(root))+') (subpath '+json.dumps(runtime)+')))))\n'
            for private in ('.ssh','.aws','.config','.cache/huggingface','.huggingface','Library/Keychains'):
                profile+='(deny file-read* (subpath '+json.dumps(str(Path.home()/private))+'))\n'
            for private in ('data','secrets','validation','docs'):
                profile+='(deny file-read* (subpath '+json.dumps(str(root/private))+'))\n'
            profile+='(allow file-write* (subpath '+json.dumps(str(directory))+') (literal "/dev/null"))\n'
            self.ws.write(rel+'/browser.sb',profile.encode())
            env={'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1',
                 'PYTHONPATH':str(root/'src'),'TMPDIR':str(directory),'XDG_CACHE_HOME':str(directory/'cache'),
                 'PLAYWRIGHT_BROWSERS_PATH':str(root/'cache/browser'),'DEBUG':'pw:browser'}
            args=['/usr/bin/sandbox-exec','-f',str(directory/'browser.sb'),str(root/'environments/research/.venv/bin/python'),
                  '-B','-m','agent_research_intelligence.connectors.browser',str(directory)]
            started=time.monotonic();peak=0;network=[];result=None;requests=0;received=0
            with (directory/'stderr.log').open('w') as err:
                proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,env=env,cwd=root,start_new_session=True,text=True)
                selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
                try:
                    while True:
                        budget.check()
                        rss=0
                        try:members=[psutil.Process(proc.pid),*psutil.Process(proc.pid).children(recursive=True)]
                        except psutil.NoSuchProcess:members=[]
                        for member in members:
                            try:rss+=member.memory_info().rss
                            except psutil.NoSuchProcess:pass
                            except psutil.AccessDenied as exc:raise AcquisitionError('BROWSER_RESOURCE_OBSERVATION_DENIED') from exc
                        rss+=psutil.Process(os.getpid()).memory_info().rss
                        peak=max(peak,rss)
                        if rss>=6*GIB:raise AcquisitionError('BROWSER_RSS_LIMIT')
                        if time.monotonic()-started>90:raise AcquisitionError('BROWSER_DEADLINE')
                        if not selector.select(.2):continue
                        line=proc.stdout.readline(12*1024**2)
                        if not line:
                            if proc.poll() is not None:break
                            continue
                        msg=json.loads(line)
                        if msg['event']=='FETCH':
                            requests+=1;u=msg['url'];answer={'ok':False}
                            try:
                                check_browser_request(url,msg,requests)
                                r=self.http.get(u);received+=len(r.body)
                                if received>16*1024**2:raise AcquisitionError('BROWSER_BYTE_LIMIT')
                                answer={'ok':True,'status':r.status,'mime':r.headers.get('content-type','text/html'),'body':base64.b64encode(r.body).decode()}
                                network.append({'url':u,'status':r.status,'bytes':len(r.body)})
                            except (BoundaryError,AcquisitionError) as exc:network.append({'url':u,'denied':str(exc)})
                            proc.stdin.write(json.dumps(answer)+'\n');proc.stdin.flush()
                        elif msg['event']=='RESULT':result=msg;break
                        elif msg['event']=='ERROR':raise AcquisitionError('BROWSER_FAILED_'+msg['reason'][:100])
                    if result is None:raise AcquisitionError('BROWSER_FAILED_NO_RESULT')
                    proc.wait(timeout=10)
                    if proc.returncode:raise AcquisitionError('BROWSER_WORKER_EXIT')
                    artifact='data/artifacts/browser/'+job
                    for name in ('page.png','page.html'):
                        self.ws.write(artifact+'/'+name,self.ws.read(rel+'/'+name))
                    result.update({'job':job,'network':network,'elapsed_s':time.monotonic()-started,'peak_process_tree_rss_bytes':peak,
                                   'sandbox':'MACOS_SEATBELT_NETWORK_DENIED_WRITES_OWN_JOB_ONLY','chromium_nested_sandbox':False,
                                   'nested_sandbox_reason':'macOS rejects sandbox initialization inside inherited Seatbelt; outer OS policy remains enforced', 'cookies':'NEW_ANONYMOUS_CONTEXT',
                                   'authority':'EXTERNAL_EVIDENCE','screenshot':artifact+'/page.png','html':artifact+'/page.html',
                                   'snapshot_retain_until':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()})
                    self.ws.write('data/browser/'+job+'.json',json.dumps(result,ensure_ascii=False,indent=2).encode())
                    return result
                finally:
                    selector.close()
                    if proc.poll() is None:
                        os.killpg(proc.pid,signal.SIGTERM)
                        try:proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
                    self.ws.write(rel+'/broker-receipt.json',json.dumps({'requests':network,'peak_rss_bytes':peak,'returncode':proc.returncode,'elapsed_s':time.monotonic()-started}).encode())


def worker(directory):
    import sys
    import socket
    from playwright.sync_api import sync_playwright
    directory=Path(directory);url=json.loads((directory/'request.json').read_text())['url']
    def send(value):print(json.dumps(value),flush=True)
    try:
        probes={}
        root=directory.parents[2]
        for name,action in [
            ('network_denied',lambda:socket.create_connection(('1.1.1.1',443),timeout=1)),
            ('outside_job_write_denied',lambda:(root/'tmp/browser-outside-job-probe').write_text('FIXTURE')),
            ('private_data_read_denied',lambda:(root/'data/intake/first_research_question.json').open('rb'))]:
            try:
                value=action()
                if hasattr(value,'close'):value.close()
                raise RuntimeError('Sandbox probe unexpectedly allowed: '+name)
            except PermissionError:probes[name]=True
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,chromium_sandbox=False,
                args=['--disable-gpu','--disable-background-networking','--disable-component-update','--disable-sync',
                      '--disable-features=WebRtcHideLocalIpsWithMdns','--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            context=browser.new_context(service_workers='block',accept_downloads=False,permissions=[])
            context.route_web_socket('**/*',lambda ws:ws.close())
            def route(r):
                request=r.request
                send({'event':'FETCH','url':request.url,'method':request.method,'resource_type':request.resource_type})
                a=json.loads(sys.stdin.readline())
                if a['ok']:r.fulfill(status=a['status'],content_type=a['mime'],body=base64.b64decode(a['body']))
                else:r.abort('blockedbyclient')
            context.route('**/*',route)
            page=context.new_page()
            context.on('page',lambda other:other.close() if other!=page else None)
            page.on('dialog',lambda d:d.dismiss())
            page.goto(url,wait_until='domcontentloaded',timeout=45000)
            # Bounded DOM observation; no clicks or model-provided JS.
            text=page.locator('body').inner_text(timeout=5000)
            html=page.content();(directory/'page.html').write_text(html)
            page.screenshot(path=str(directory/'page.png'),full_page=False)
            result={'event':'RESULT','url':page.url,'title':page.title(),'text':text[:100000],
                    'browser_version':browser.version,'pages':len(context.pages),'cookie_count':len(context.cookies()),'os_sandbox_probes':probes}
            context.close();browser.close();send(result)
    except Exception as exc:send({'event':'ERROR','reason':type(exc).__name__+': '+str(exc)[:300]})


if __name__=='__main__':
    import sys
    worker(sys.argv[1])

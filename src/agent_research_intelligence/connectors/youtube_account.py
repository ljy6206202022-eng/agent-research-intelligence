"""Optional YouTube read-only OAuth. Fixed hosts, endpoints and scope only.

No ambient credentials, redirects, cookies or subscription mutation.
An explicit User grant may route only the two backend hosts via local proxy.
The authenticated adapter is separate from the anonymous PublicHTTP adapter.
"""
import base64
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import socket
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from agent_research_intelligence.connectors.public_http import PublicHTTP, AcquisitionError
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical

SCOPE = 'https://www.googleapis.com/auth/youtube.readonly'
GRANT = 'config/permissions/youtube-readonly.json'
CLIENT = 'secrets/youtube-client.json'
TOKEN = 'secrets/youtube-token.json'
PROXY_GRANT = 'config/permissions/youtube-backend-proxy.json'
BACKEND_HOSTS = ['oauth2.googleapis.com', 'www.googleapis.com']
ENDPOINTS = {
    'subscriptions': {'part', 'mine', 'maxResults', 'pageToken'},
    'channels': {'part', 'id', 'maxResults'},
    'playlistItems': {'part', 'playlistId', 'maxResults', 'pageToken'},
    'videos': {'part', 'id', 'maxResults'},
    'search': {'part', 'q', 'type', 'maxResults', 'pageToken', 'order'},
}


class YouTubeAccount:
    def __init__(self, workspace, *, transport=None):
        self.ws = workspace
        self.transport = transport or self._transport()
        self.trace = []
        self._budget = None
        self.resource_probe = None

    def _transport(self):
        if not (self.ws.root/PROXY_GRANT).exists():
            return PublicHTTP(timeout=20, max_bytes=2*1024**2)
        from .youtube_proxy import APPROVED_PROXY, _ProxyPinnedConnection
        try:
            g = json.loads(self.ws.read(PROXY_GRANT))
        except (OSError, ValueError):
            raise BoundaryError('Invalid backend proxy grant') from None
        if not (g.get('status')=='USER_APPROVED' and g.get('scope')==SCOPE
                and g.get('proxy')==APPROVED_PROXY and g.get('hosts')==BACKEND_HOSTS
                and g.get('account_mutation') is False and g.get('authorization_basis')):
            raise BoundaryError('Invalid backend proxy grant')
        class BackendProxyConnection(_ProxyPinnedConnection):
            route = 'EXPLICIT_YOUTUBE_BACKEND_PROXY'
            def __init__(self, host, address, timeout):
                if host not in BACKEND_HOSTS:
                    raise BoundaryError('Backend proxy host denied')
                super().__init__(host, address, timeout)
        # Preserve public DNS validation, pinned IP, verified TLS and no redirects.
        return PublicHTTP(timeout=20, max_bytes=2*1024**2,
                          connection_factory=BackendProxyConnection)

    def status(self):
        try:
            g = json.loads(self.ws.read(GRANT))
            approved = (g.get('status')=='USER_APPROVED' and g.get('scope')==SCOPE
                        and g.get('account_mutation') is False and bool(g.get('authorization_basis'))
                        and g.get('hosts')==['accounts.google.com','oauth2.googleapis.com','www.googleapis.com'])
        except (OSError, ValueError):
            approved = False
        return {'status':'READ_PERMISSION_APPROVED' if approved else 'AUTH_REQUIRED',
                'scope':SCOPE,'account_mutation':False,'uses_ambient_credentials':False,
                'client_present':(self.ws.root/CLIENT).is_file(),
                'token_present':(self.ws.root/TOKEN).is_file()}

    def _gate(self):
        if self.status()['status']!='READ_PERMISSION_APPROVED':
            raise AcquisitionError('AUTH_REQUIRED')

    def _secret(self, path):
        self._gate()
        p=self.ws.checked_path(path)
        if not p.exists():raise AcquisitionError('OAUTH_CLIENT_REQUIRED' if path==CLIENT else 'AUTH_REQUIRED')
        if p.stat().st_mode & 0o077:raise BoundaryError('OAuth files require mode 0600')
        return json.loads(self.ws.read(path,limit=16384))

    def _request(self, host, path, *, form=None, token=None):
        if (host,path.split('?')[0]) not in {('oauth2.googleapis.com','/token')} | {
                ('www.googleapis.com','/youtube/v3/'+e) for e in ENDPOINTS}:
            raise BoundaryError('OAuth endpoint denied')
        # Same system/disk policy as anonymous acquisition; not a new bypass.
        self._resource_guard()
        _, h, address=self.transport._destination('https://'+host+path)
        conn=self.transport.connection_factory(h,address,self.transport.timeout)
        started=time.monotonic();entry={'host':host,'endpoint':path.split('?')[0],
                                      'route':getattr(conn,'route','DIRECT')}
        try:
            headers={'Accept-Encoding':'identity','User-Agent':'Research-Intel-readonly/0.1'}
            if token:
                if not isinstance(token,str) or len(token)>8192 or any(ord(c)<33 or ord(c)>126 for c in token):
                    raise BoundaryError('Invalid OAuth token format')
                headers['Authorization']='Bearer '+token
            if form is not None:
                if host!='oauth2.googleapis.com' or token:raise BoundaryError('OAuth exchange only')
                headers['Content-Type']='application/x-www-form-urlencoded'
            conn.request('POST' if form is not None else 'GET',path,
                         body=urlencode(form).encode() if form is not None else None,headers=headers)
            response=conn.getresponse();entry['status']=response.status
            if response.status!=200:
                # Never expose provider body, token, client details or query.
                code='AUTH_REQUIRED' if response.status in (400,401,403) else 'RATE_LIMITED' if response.status==429 else 'NOT_FOUND' if response.status==404 else 'OAUTH_HTTP_FAILED'
                if response.status==403 and host=='www.googleapis.com':
                    try:
                        error=json.loads(response.read(16384))
                        reasons={e.get('reason') for e in error.get('error',{}).get('errors',[])}
                        if reasons & {'quotaExceeded','dailyLimitExceeded'}:code='QUOTA_EXCEEDED'
                        elif reasons & {'rateLimitExceeded','userRateLimitExceeded'}:code='RATE_LIMITED'
                    except (ValueError,AttributeError,TypeError):pass
                entry['error_code']=code
                raise AcquisitionError(code)
            if response.getheader('Content-Encoding','identity') not in ('identity',''):
                raise AcquisitionError('UNSUPPORTED_CONTENT_ENCODING')
            body=response.read(2*1024**2+1);entry['bytes']=len(body)
            if len(body)>2*1024**2:raise AcquisitionError('CONTENT_TOO_LARGE')
            value=json.loads(body)
            if not isinstance(value,dict):raise AcquisitionError('OAUTH_RESPONSE_SHAPE')
            return value
        except (OSError,http.client.HTTPException,ValueError) as exc:
            raise AcquisitionError('OAUTH_TRANSPORT_OR_PARSE_FAILED') from None
        finally:
            conn.close();entry['elapsed_s']=round(time.monotonic()-started,4)
            self.trace.append(entry)

    def _resource_guard(self):
        import psutil
        from agent_research_intelligence.acquisition.audio_runtime import Budget
        probe = getattr(self, 'resource_probe', None)
        if probe:
            import inspect
            frame = inspect.currentframe().f_back
            probe.note_guard(frame.f_code.co_name + ':' + str(frame.f_lineno))
        if self._budget is None:
            callback = probe.stop_snapshot if probe else None
            self._budget = (Budget(self.ws,psutil,on_memory_stop=callback)
                            if callback else Budget(self.ws,psutil))
        self._budget.check()

    def _client(self):
        c=self._secret(CLIENT).get('installed',{})
        if not c.get('client_id') or not c.get('client_secret'):
            raise AcquisitionError('DESKTOP_OAUTH_CLIENT_REQUIRED')
        return {'client_id':c['client_id'],'client_secret':c['client_secret']}

    def authorize(self, *, display=print):
        """Explicit foreground loopback; prints URL, never opens/controls a browser."""
        self._gate();client=self._client();self._resource_guard()
        state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        received={}
        class Callback(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                parsed=urlsplit(self.path);q=parse_qs(parsed.query)
                valid=(parsed.path=='/oauth/callback' and q.get('state')==[state] and len(q.get('code',[]))==1)
                if valid:received['code']=q['code'][0]
                self.send_response(200 if valid else 400);self.end_headers()
                self.wfile.write(b'Authorization received. Return to the research tool.' if valid else b'Invalid callback.')
        with HTTPServer(('127.0.0.1',0),Callback) as server:
            server.timeout=2
            redirect=f'http://127.0.0.1:{server.server_port}/oauth/callback'
            url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(dict(
                client_id=client['client_id'],redirect_uri=redirect,response_type='code',scope=SCOPE,
                access_type='offline',prompt='consent',state=state,code_challenge=challenge,code_challenge_method='S256'))
            display(url)
            deadline=time.monotonic()+180
            while not received and time.monotonic()<deadline:server.handle_request()
        if not received:raise AcquisitionError('OAUTH_CALLBACK_TIMEOUT')
        result=self._request('oauth2.googleapis.com','/token',form={**client,'code':received['code'],
            'code_verifier':verifier,'redirect_uri':redirect,'grant_type':'authorization_code'})
        self._save_token(result)
        return {'status':'AUTHORIZED_READ_ONLY','scope':SCOPE,'token_disclosed':False}

    def _save_token(self, value, previous=None):
        scopes=set(value.get('scope','').split())
        if scopes!={SCOPE} or value.get('token_type','').lower()!='bearer' or not value.get('access_token'):
            raise AcquisitionError('OAUTH_SCOPE_MISMATCH')
        stored={k:value[k] for k in ('access_token','refresh_token','scope','token_type') if k in value}
        if 'refresh_token' not in stored and previous:stored['refresh_token']=previous.get('refresh_token')
        stored['expires_at']=time.time()+int(value.get('expires_in',0))
        self.ws.write(TOKEN,canonical(stored),replace=True)

    def read(self, endpoint, params):
        self._gate()
        if endpoint not in ENDPOINTS or set(params)-ENDPOINTS[endpoint]:raise BoundaryError('YouTube read contract denied')
        if endpoint=='subscriptions' and params.get('mine')!='true':raise BoundaryError('Subscriptions must use mine=true')
        if not 1<=int(params.get('maxResults',50))<=50:raise BoundaryError('Invalid API page size')
        token=self._secret(TOKEN)
        if token.get('scope')!=SCOPE:raise AcquisitionError('OAUTH_SCOPE_MISMATCH')
        if token.get('expires_at',0)<=time.time()+30:
            if not token.get('refresh_token'):raise AcquisitionError('AUTH_REQUIRED')
            response=self._request('oauth2.googleapis.com','/token',form={**self._client(),
                'refresh_token':token['refresh_token'],'grant_type':'refresh_token'})
            self._save_token(response,token);token=self._secret(TOKEN)
        return self._request('www.googleapis.com','/youtube/v3/'+endpoint+'?'+urlencode(params),token=token['access_token'])

    def subscriptions(self, *, max_pages=10):
        if not 1<=max_pages<=10:raise BoundaryError('Bounded subscription pages required')
        items=[];cursor=None;seen=set()
        for _ in range(max_pages):
            params={'part':'snippet','mine':'true','maxResults':50}
            if cursor:params['pageToken']=cursor
            page=self.read('subscriptions',params)
            for item in page.get('items',[]):
                sn=item['snippet'];cid=sn['resourceId']['channelId']
                if cid not in seen:
                    seen.add(cid);items.append({'channel_id':cid,'title':sn['title'],
                        'description':sn.get('description',''),'subscription_id':item['id']})
            cursor=page.get('nextPageToken')
            if not cursor:break
        # Account response stays PRIVATE; no token or raw OAuth response is included.
        receipt={'at':datetime.now(timezone.utc).isoformat(),'kind':'AUTHENTICATED_SUBSCRIPTION_READ',
                 'classification':'PRIVATE','items':items,'complete':not bool(cursor),'requests':self.trace,
                 'scope':SCOPE,'account_mutation':False}
        path='data/youtube/account/'+secrets.token_hex(16)+'.json'
        self.ws.write(path,canonical(receipt))
        return {'status':'READ_COMPLETE' if not cursor else 'PARTIAL_PAGE_BUDGET','artifact':path,
                'sha256':hashlib.sha256(canonical(receipt)).hexdigest(),'channels':len(items)}

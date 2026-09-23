import base64
import json
import re
from urllib.parse import urlsplit, quote, unquote

from .public_http import AcquisitionError, PublicHTTP
from agent_research_intelligence.governance.paths import BoundaryError


class GitHub:
    def __init__(self, http: PublicHTTP):
        self.http = http

    def fetch(self, url: str) -> dict:
        p = urlsplit(url)
        parts = p.path.strip("/").split("/")
        if p.scheme != "https" or p.hostname != "github.com" or p.username or p.password or p.port not in {None, 443} or len(parts) < 2:
            raise BoundaryError("Public GitHub repository URL required")
        owner, name = parts[:2]
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) and x not in {".", ".."} for x in (owner, name)):
            raise BoundaryError("Invalid repository identifier")
        api = f"https://api.github.com/repos/{owner}/{name}"
        file_path=None;requested_ref=None
        if len(parts)>2:
            if len(parts)<5 or parts[2]!='blob':
                raise BoundaryError('Use repository root or an explicit blob URL; unsupported resource is not replaced by README')
            requested_ref=unquote(parts[3]);file_path='/'.join(unquote(x) for x in parts[4:])
            if not requested_ref or any(x in ('','..','.') for x in file_path.split('/')) or '\\' in file_path:
                raise BoundaryError('Invalid GitHub file reference')
        try:
            meta = json.loads(self.http.get(api).body)
            commit = json.loads(self.http.get(api + "/commits/" + quote(requested_ref or meta["default_branch"], safe="")).body)
            sha = commit["sha"]
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("Invalid commit")
            endpoint='/contents/'+quote(file_path,safe='/') if file_path else '/readme'
            readme = json.loads(self.http.get(api + endpoint+"?ref=" + sha).body)
            if file_path and readme.get('path')!=file_path:raise ValueError('Requested file identity mismatch')
            if readme.get('encoding','base64')!='base64':raise ValueError('Unsupported GitHub file encoding')
            content = base64.b64decode(readme["content"], validate=False).decode("utf-8", errors="replace")
        except (KeyError, ValueError, TypeError) as exc:
            raise AcquisitionError("GITHUB_RESPONSE_INVALID") from exc
        path = readme["path"]
        return {"repository": f"{owner}/{name}", "source_url": url, "commit": sha,
                "readme_path": path, "readme": content,
                "content_kind":"EXPLICIT_FILE" if file_path else "README", "requested_ref":requested_ref,
                "locator": f"https://github.com/{owner}/{name}/blob/{sha}/{quote(path, safe='/')}",
                "license": (meta.get("license") or {}).get("spdx_id"),
                "authority": "EXTERNAL_EVIDENCE", "commands_executed": False}

    def research_snapshot(self, url: str) -> dict:
        """Bounded supplementary public surfaces; absence/failure is not poor maintenance."""
        result=self.fetch(url)
        base='https://api.github.com/repos/'+result['repository']
        surfaces={}
        for name,suffix in [('releases','/releases?per_page=3'),('issues','/issues?state=open&per_page=3'),
                            ('pull_requests','/pulls?state=all&per_page=3'),
                            ('activity','/commits?per_page=3'),
                            ('tree','/git/trees/'+result['commit']+'?recursive=1')]:
            try:
                response=self.http.get(base+suffix);value=json.loads(response.body)
                surfaces[name]={'url':response.url,'status':'FETCHED','content':value}
            except (AcquisitionError,ValueError) as exc:
                surfaces[name]={'url':base+suffix,'status':'UNAVAILABLE','reason':str(exc)}
        result['surfaces']=surfaces
        tree=surfaces.get('tree',{}).get('content',{})
        paths=[x['path'] for x in tree.get('tree',[]) if x.get('type')=='blob'] if isinstance(tree,dict) else []
        result['test_signals']={'paths':[p for p in paths if re.search(r'(^|/)(tests?|specs?)(/|\.)',p)][:30],
            'tests_run':False,'coverage':None,'meaning':'Repository file signals, not passing-test evidence'}
        result['dependency_signals']={'paths':[p for p in paths if p.rsplit('/',1)[-1] in
            ('pyproject.toml','requirements.txt','package.json','Cargo.toml','go.mod','uv.lock','package-lock.json')][:30],
            'dependency_health':'UNKNOWN'}
        result['maintainer_signals']={'api_activity':surfaces.get('activity'),'reliability':'UNKNOWN','historical_utility':None}
        result['maintenance_conclusion']='UNASSESSED; sampled public activity is not a reliability score'
        result['tests_executed']=False
        return result

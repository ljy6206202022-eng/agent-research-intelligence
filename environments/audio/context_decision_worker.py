"""stdin-only deterministic context decision under deny-network/data sandbox."""
import json,sys,os
from agent_research_intelligence.governance.paths import APP_ROOT
from agent_research_intelligence.acquisition.speaker_context import decide
if __name__=='__main__':
    raw=sys.stdin.buffer.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise SystemExit(2)
    # Fail closed if the launcher omitted its reference/data read-denial sandbox.
    for directory in ('validation','data'):
        try:
            fd=os.open(APP_ROOT/directory,os.O_RDONLY)
        except PermissionError:
            continue
        else:
            os.close(fd);raise SystemExit('REFERENCE_ISOLATION_NOT_ACTIVE')
    print(json.dumps({'reference_stores':'OS_READ_DENIED','input_channel':'STDIN_PROJECTED_MATERIALS_ONLY','network':'OS_DENIED'}),file=sys.stderr)
    request=json.loads(raw)
    if set(request)!={'binding','bundle','review'}:raise SystemExit(2)
    print(json.dumps(decide(**request),ensure_ascii=False,allow_nan=False))

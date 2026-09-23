import argparse
import json
from pathlib import Path
import sys
import hashlib
from uuid import uuid4

from agent_research_intelligence.governance.paths import APP_ROOT, BoundaryError, Workspace
from agent_research_intelligence.governance.policy import POLICY_HASH, POLICY, verify_policy
from agent_research_intelligence.governance.project_reader import snapshot_project_file
from agent_research_intelligence.storage.database import Store
from agent_research_intelligence.connectors.public_http import AcquisitionError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local-first evidence-backed research")
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Read policy status; does not create a database")
    commands.add_parser("init-store", help="Initialize only this tool's local database")
    commands.add_parser("init-workspace", help="Initialize a local workspace and its safety policy")
    commands.add_parser("doctor", help="Check local setup without displaying credentials")
    initial=commands.add_parser('initial-read',help='Sequential catalog reads plus independent account status')
    initial.add_argument('--question-id',required=True)
    public=commands.add_parser('research-public',help='Explicit public-query research and local dossier')
    public.add_argument('--question',required=True)
    public.add_argument('--terms',nargs='+',required=True)
    public.add_argument('--github-query')
    public.add_argument('--paper-query')
    public.add_argument('--crossref-query')
    public.add_argument('--allow-public-network',action='store_true')
    commands.add_parser("backup", help="Consistent backup of this tool's database")
    commands.add_parser('contract-list',help='Inspect all 32 Versioned Appendix B contracts')
    legacy=commands.add_parser('project-discovery',help='Project sealed discovery artifacts into unified contracts; REUSED_REAL, no new research claim')
    legacy.add_argument('--job',required=True)
    contract=commands.add_parser('contract',help='Call a Versioned contract with an owned JSON request')
    contract.add_argument('name');contract.add_argument('--request',required=True)
    for action in ('read','search','write'):
        cp=commands.add_parser('catalog-'+action,help='Versioned versioned RQ/source/evidence records, internal only')
        if action=='read':
            cp.add_argument('id');cp.add_argument('--revision',type=int)
        elif action=='search':
            cp.add_argument('kind');cp.add_argument('--terms',nargs='*',default=[])
        else:
            cp.add_argument('--request',required=True,help='Owned JSON: kind, payload, reason, optional identifier/expected_revision')
    for action in ('status','authorize','subscriptions'):
        yp=commands.add_parser('youtube-account-'+action,help='Explicit read-only account gate; never subscribe/unsubscribe')
        if action=='subscriptions':yp.add_argument('--max-pages',type=int,default=10)
    yi=commands.add_parser('youtube-import-subscriptions',help='Import a real private account-read receipt with provenance')
    yi.add_argument('--receipt',required=True)
    yd=commands.add_parser('youtube-discover',help='Approved public RQ query -> candidate channels; recommend subscribe only')
    yd.add_argument('--brief',required=True);yd.add_argument('--approval',required=True)
    profile=commands.add_parser('youtube-profile',help='Sample three to five recent videos for a topic-specific source assessment')
    profile.add_argument('--source',required=True);profile.add_argument('--rq',required=True);profile.add_argument('--samples',type=int,default=3)
    yr=commands.add_parser('youtube-research',help='Known channels -> recent metadata -> a few transcripts/resources -> evidence/dossier')
    yr.add_argument('--rq',required=True)
    yr.add_argument('--brief',help='Existing approved PUBLIC brief for coverage-triggered discovery')
    yr.add_argument('--approval',help='Exact brief-hash approval')
    for opt,default in [('max-sources',20),('max-metadata',30),('max-light',5),('max-deep',2)]:yr.add_argument('--'+opt,type=int,default=default)
    yr.add_argument('--asr-profile',choices=['AUDIO_ASR_PROFILE_EN_TECH_PARAKEET_v0.1'])
    yr.add_argument('--material-type',choices=['UNKNOWN','ENGLISH_TECHNICAL_LECTURE'],default='UNKNOWN')
    yr.add_argument('--diarization-provider',choices=['community1'])
    snap = commands.add_parser("snapshot-project", help="Explicit allowlisted project file, stored privately")
    snap.add_argument("path", type=Path)
    research = commands.add_parser("research", help="Public video read; private extractive dossier, no cloud model")
    research.add_argument("--intake", required=True, help="Workspace-relative User intake JSON")
    research.add_argument("--source", required=True, help="Explicit initial YouTube video URL")
    research.add_argument("--terms", nargs="+", required=True, help="Local excerpt matching terms; never sent to cloud")
    research.add_argument("--youtube-proxy", help="Explicit approved source proxy URL; never inherits system/environment proxy")
    discovery=commands.add_parser('discover-research',help='discovery: local question, explicitly approved public brief, source discovery and located evidence')
    for name in ('intake','brief','approval','seeds'):
        discovery.add_argument('--'+name,required=True,help='Workspace-relative JSON')
    browser=commands.add_parser('browse-public',help='Dedicated anonymous browser; controlled read-only HTTPS broker')
    browser.add_argument('url')
    collect=commands.add_parser('collect-candidates',help='Collect explicit IDs from a saved discovery run; no arbitrary new source injection')
    collect.add_argument('--job',required=True);collect.add_argument('--ids',nargs='+',required=True)
    collect.add_argument('--include-pdf',action='store_true',help='Follow the first explicit PDF link from a discovered paper')
    review=commands.add_parser('review-dossier',help='Bind a local source-grounded review; output remains PRIVATE RESEARCH_OUTPUT')
    review.add_argument('--review',required=True)
    register=commands.add_parser('watch-register',help='Register explicit public sources against an existing dossier')
    register.add_argument('--config',required=True)
    for action in ('poll','run','stop','resume','status','review'):
        wp=commands.add_parser('watch-'+action,help='watch bounded internal evidence watch: '+action)
        wp.add_argument('--watch',required=True)
        if action=='poll':wp.add_argument('--force',action='store_true',help='Explicit one-off poll, respects failure backoff')
        if action=='run':
            wp.add_argument('--cycles',type=int,default=1);wp.add_argument('--interval',type=int,default=60)
        if action=='review':wp.add_argument('--event',required=True);wp.add_argument('--review',required=True)
    for action in ('import','inventory','baseline','assess','diagnose','renewal','disable','enable'):
        hp=commands.add_parser('health-'+action,help='health explicit local PRIVATE health: '+action)
        hp.add_argument('--mode',choices=('REAL','FIXTURE'),default='REAL')
        if action=='import':hp.add_argument('--manifest',required=True)
        if action=='assess':hp.add_argument('--baseline',required=True)
        if action=='diagnose':hp.add_argument('--record',required=True)
        if action=='renewal':
            hp.add_argument('--assessment',required=True);hp.add_argument('--diagnosis',required=True)
        if action in ('enable','disable'):hp.add_argument('--reason',required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init-workspace':
            args.root.mkdir(parents=True,exist_ok=True)
        workspace = Workspace(args.root)
        if args.command == 'init-workspace' and not (workspace.root/'config/policies/safety.json').exists():
            from agent_research_intelligence.governance.policy import canonical
            workspace.write('config/policies/safety.json',canonical(POLICY))
        verify_policy(workspace)
        if args.command in ('init-workspace','init-store'):
            with Store(workspace) as store:
                result={'status':'PASS','schema_version':1,'audit_events':store.verify_audit()}
        elif args.command == 'doctor':
            import importlib.util,platform,psutil,shutil
            checks=[]
            def add(name,state,detail=None):checks.append({'name':name,'status':state,'detail':detail})
            add('python','PASS' if sys.version_info[:2]==(3,11) else 'BLOCKED',platform.python_version())
            add('policy','PASS')
            with Store(workspace) as store:add('database','PASS',str(store.verify_audit())+' audit events')
            for name in ('ffmpeg','yt-dlp','node'):
                add(name,'PASS' if shutil.which(name) else 'OPTIONAL_MISSING')
            for name,module in [('audio','faster_whisper'),('browser','playwright')]:
                add(name,'PASS' if importlib.util.find_spec(module) else 'OPTIONAL_MISSING')
            free=shutil.disk_usage(workspace.root).free
            available=psutil.virtual_memory().available
            add('free_disk','PASS' if free>=POLICY['limits']['free_disk_min_gib']*2**30 else 'BLOCKED',str(free//2**20)+' MiB')
            add('available_memory','PASS' if available>=POLICY['limits']['free_memory_min_gib']*2**30 else 'BLOCKED',str(available//2**20)+' MiB')
            add('youtube_oauth','PASS' if (workspace.root/'secrets/youtube-token.json').is_file() else 'OPTIONAL_MISSING')
            result={'status':'BLOCKED' if any(c['status']=='BLOCKED' for c in checks) else 'PASS','checks':checks}
        elif args.command == 'research-public':
            from agent_research_intelligence.governance.policy import canonical
            from agent_research_intelligence.research.discovery import DiscoveryResearch
            from agent_research_intelligence.research.catalog import Catalog
            if not args.allow_public_network:raise BoundaryError('Explicit public network permission required')
            queries=[{'provider':provider,'text':query} for provider,query in (
                ('github',args.github_query),('arxiv',args.paper_query),('crossref',args.crossref_query)) if query]
            if not queries:raise BoundaryError('At least one explicit public query required')
            catalog=Catalog(workspace)
            question=catalog.put('question',{'problem':args.question,'current_context':'',
                'constraints':['Local read/analyze/propose only'],'need_to_learn':[args.question],
                'search_terms':args.terms},reason='Explicit research request')
            # Store lease is exclusive: all catalog reads are sequential in this invocation.
            prior={kind:len(catalog.search(kind,args.terms)) for kind in ('evidence','dossier','source')}
            ident=uuid4().hex;prefix='data/requests/'+ident
            brief={'classification':'PUBLIC','queries':queries,'local_terms':args.terms}
            approval={'authority':'USER_AUTHORIZED_PUBLIC_RESEARCH',
                'brief_sha256':hashlib.sha256(canonical(brief)).hexdigest(),
                'source_message':'Explicit CLI --allow-public-network and public query arguments'}
            for name,value in [('intake',{'original_question':args.question}),('brief',brief),('approval',approval),('seeds',[])]:
                workspace.write(prefix+'/'+name+'.json',canonical(value))
            result=DiscoveryResearch(workspace).run(*(prefix+'/'+name+'.json' for name in ('intake','brief','approval','seeds')))
            result.update(question_id=question['id'],prior_record_counts=prior,request_id=ident)
        elif args.command == 'initial-read':
            from agent_research_intelligence.research.initial_reads import run
            result=run(workspace,args.question_id)
        elif args.command == "status":
            result = {"authority": "LOCAL_RESEARCH_ONLY", "policy_hash": POLICY_HASH,
                      "gates": POLICY["gates"], "network_transport": "PUBLIC_HTTPS_READ_ONLY"}
        elif args.command=='contract-list':
            import inspect
            from agent_research_intelligence.research.contracts import ResearchContracts,NAMES
            result={'contracts':{n:{'signature':str(inspect.signature(getattr(ResearchContracts,n))),
                     'entry':'INTERNAL_LEASED_PIPELINE' if n=='AudioExtract' else 'CLI_JSON_OR_PYTHON'} for n in NAMES}}
        elif args.command=='contract':
            from agent_research_intelligence.research.contracts import invoke
            result=invoke(workspace,args.name,json.loads(workspace.read(args.request)))
        elif args.command=='project-discovery':
            from agent_research_intelligence.research.legacy_bridge import project_discovery
            result=project_discovery(workspace,args.job)
        elif args.command.startswith('catalog-'):
            from agent_research_intelligence.research.catalog import Catalog
            catalog=Catalog(workspace)
            if args.command=='catalog-read':result=catalog.read(args.id,args.revision)
            elif args.command=='catalog-search':result={'records':catalog.search(args.kind,args.terms)}
            else:result=catalog.put(**json.loads(workspace.read(args.request)))
        elif args.command.startswith('youtube-account-'):
            from agent_research_intelligence.connectors.youtube_account import YouTubeAccount
            account=YouTubeAccount(workspace)
            if args.command.endswith('-status'):result=account.status()
            elif args.command.endswith('-authorize'):result=account.authorize(display=lambda url:print(url,flush=True))
            else:result=account.subscriptions(max_pages=args.max_pages)
        elif args.command in ('youtube-research','youtube-discover','youtube-import-subscriptions','youtube-profile'):
            from agent_research_intelligence.research.youtube_research import YouTubeResearch
            yt=YouTubeResearch(workspace)
            if args.command=='youtube-import-subscriptions':result=yt.import_subscriptions(args.receipt)
            elif args.command=='youtube-discover':result=yt.discover(args.brief,args.approval)
            elif args.command=='youtube-profile':result=yt.profile(args.source,args.rq,samples=args.samples)
            else:result=yt.workflow(args.rq,brief_path=args.brief,approval_path=args.approval,max_sources=args.max_sources,max_metadata=args.max_metadata,
                max_light=args.max_light,max_deep=args.max_deep,asr_profile=args.asr_profile,
                material_type=args.material_type,diarization_provider=args.diarization_provider)
        elif args.command.startswith('health-'):
            from agent_research_intelligence.health.service import Health
            health=Health(workspace,mode=args.mode)
            action=args.command[7:]
            if action=='import':result=health.import_signals(args.manifest)
            elif action=='inventory':result=health.inventory()
            elif action=='baseline':result=health.baseline()
            elif action=='assess':result=health.assess(args.baseline)
            elif action=='diagnose':result=health.diagnose(args.record)
            elif action=='renewal':result=health.renewal(args.assessment,args.diagnosis)
            else:result=health.control(action=='enable',args.reason)
        elif args.command.startswith('watch-'):
            from agent_research_intelligence.watch.service import Watch
            watch=Watch(workspace)
            action=args.command[6:]
            if action=='register':result=watch.register(args.config)
            elif action=='poll':result=watch.poll(args.watch,force=args.force)
            elif action=='run':result=watch.run(args.watch,args.cycles,args.interval)
            elif action in ('stop','resume'):result=watch.control(args.watch,action)
            elif action=='review':result=watch.review(args.watch,args.event,args.review)
            else:
                state=watch.state(args.watch)
                result={k:state[k] for k in ('id','watch_status','rq_status','watch_until','state_revision','current_dossier')}
        elif args.command=='discover-research':
            from agent_research_intelligence.research.discovery import DiscoveryResearch
            result=DiscoveryResearch(workspace).run(args.intake,args.brief,args.approval,args.seeds)
            from agent_research_intelligence.research.legacy_bridge import project_discovery
            result['unified_contracts']=project_discovery(workspace,result['job'],classification='REAL')
        elif args.command=='browse-public':
            from agent_research_intelligence.connectors.browser import Browser
            result=Browser(workspace).fetch(args.url)
        elif args.command=='collect-candidates':
            from agent_research_intelligence.research.discovery import DiscoveryResearch
            result=DiscoveryResearch(workspace).collect_candidates(args.job,args.ids,include_pdf=args.include_pdf)
            from agent_research_intelligence.research.legacy_bridge import project_collection
            result['unified_contracts']=project_collection(workspace,args.job,result['batch'],classification='REAL')
        elif args.command=='review-dossier':
            from agent_research_intelligence.research.dossier_review import build_review
            result=build_review(workspace,args.review)
        elif args.command == "research":
            from agent_research_intelligence.research.service import ResearchQuestion, ResearchService
            intake = json.loads(workspace.read(args.intake))
            if not isinstance(intake, dict) or not isinstance(intake.get('original_question'), str):
                raise BoundaryError('Intake must contain original_question text')
            question = ResearchQuestion(original=intake['original_question'], source_url=args.source,
                constraints=('Read/analyze/propose only', 'Do not modify or resolve project lifecycle', 'Private context stays local'),
                search_terms=tuple(args.terms))
            http = None
            if args.youtube_proxy is not None:
                from agent_research_intelligence.connectors.youtube_proxy import YouTubeProxyHTTP
                http = YouTubeProxyHTTP(args.youtube_proxy)
            result = ResearchService(workspace, http=http).run(question)
        elif args.command == "snapshot-project":
            result = snapshot_project_file(workspace, args.path)
        else:
            with Store(workspace) as store:
                result = store.snapshot()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('state') == 'BLOCKED' else 0
    except (BoundaryError, OSError, ValueError, TypeError, KeyError, AcquisitionError) as exc:
        print(json.dumps({"status": "STOP", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

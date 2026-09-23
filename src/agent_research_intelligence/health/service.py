"""Immutable receipt-derived signals and frozen stratified baselines.

All collection is explicit and local. Imported historical annotations remain
annotations: hash checks establish lineage, not truth of an operator's claim.
Real and fixture namespaces cannot share baselines, diagnoses or renewal gates.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import statistics

from agent_research_intelligence.governance.paths import BoundaryError, Workspace
from agent_research_intelligence.storage.database import Store

POLICY_PATH = 'config/health/HEALTH_HEALTH_POLICY_v0.1.json'
# Replaced once when the preregistered policy is sealed. No runtime edits allowed.
POLICY_SHA256 = 'c65a982f1c2408ea324db27838cc96ef9bd447303873a59ccac74c491f7cf3c6'
CAUSES = ('MODULE / IMPLEMENTATION', 'INPUT / DATA', 'NETWORK',
          'CREDENTIAL / PERMISSION', 'UPSTREAM PROVIDER', 'RESOURCE',
          'TASK_MIX_CHANGE', 'EVALUATOR / METRIC_DRIFT', 'UNKNOWN')
STRATUM = ('capability','task_type','provider','model','version','configuration','evaluator')
UNKNOWN = (None, '', 'UNKNOWN')

def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def now():
    return datetime.now(timezone.utc).isoformat()

def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise BoundaryError('Execution time must contain timezone')
    return dt.astimezone(timezone.utc)

def wilson(successes, count, z=1.959963984540054):
    if not count:
        return None
    p = successes/count
    center = (p+z*z/(2*count))/(1+z*z/count)
    half = z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/(1+z*z/count)
    return [max(0.,center-half), min(1.,center+half)]

def summary(signals):
    outcomes = [s['success'] for s in signals if s['success'] is not None]
    metrics = {}
    for name in sorted({k for s in signals for k in s.get('metrics', {})}):
        values = [s['metrics'][name] for s in signals if isinstance(s.get('metrics',{}).get(name),(int,float)) and not isinstance(s['metrics'][name],bool)]
        if values:
            metrics[name] = {'n':len(values),'min':min(values),'median':statistics.median(values),'max':max(values)}
    return {'n':len(signals),'known_outcomes':len(outcomes),'successes':sum(outcomes),
            'success_rate':sum(outcomes)/len(outcomes) if outcomes else None,
            'success_wilson95':wilson(sum(outcomes),len(outcomes)), 'metrics':metrics}

def classify_observation(code):
    """Classify an observed failure surface, not an unobserved ultimate cause."""
    groups = {
        'NETWORK': {'AUDIO_TRANSPORT_FAILED','HTTP_TIMEOUT','CONNECTION_RESET','DNS_FAILED'},
        'CREDENTIAL / PERMISSION': {'AUDIO_ACCESS_DENIED','HTTP_401','HTTP_403','SANDBOX_DENIED'},
        'UPSTREAM PROVIDER': {'HTTP_429','HTTP_503'},
        'RESOURCE': {'AUDIO_INTEGRATION_START_RESOURCE_WAIT_12GIB','SYSTEM_MEMORY_LOW_PAUSED','DISK_FREE_LOW_PAUSED'},
        'INPUT / DATA': {'INVALID_MEDIA','UNSUPPORTED_LANGUAGE','EMPTY_INPUT'},
        'MODULE / IMPLEMENTATION': {'UNHANDLED_ZOMBIE_PROCESS','PARSER_ASSERTION_FAILED'},
        'TASK_MIX_CHANGE': {'TASK_DISTRIBUTION_CHANGED'},
        'EVALUATOR / METRIC_DRIFT': {'REFERENCE_POLICY_CHANGED'},
    }
    return next((category for category,codes in groups.items() if code in codes),'UNKNOWN')

def eligible(s):
    return s['independent_task'] and s['success'] is not None and s['occurred_at'] is not None and all(s[k] not in UNKNOWN for k in STRATUM)

def stratum_id(s):
    return digest(encoded({k:s[k] for k in STRATUM}))

class Health:
    def __init__(self, workspace: Workspace, *, mode='REAL'):
        if mode not in ('REAL','FIXTURE'):
            raise BoundaryError('Invalid health mode')
        self.ws,self.mode = workspace,mode
        raw = workspace.read(POLICY_PATH)
        if digest(raw) != POLICY_SHA256:
            raise BoundaryError('Health policy changed: versioned reviewed code binding required')
        self.policy = json.loads(raw)
        self.base = 'data/health/'+mode.lower()

    def _read(self, path):
        return json.loads(self.ws.read(path))

    def _put(self, kind, value):
        value = {**value,'classification':'PRIVATE','mode':self.mode,'policy_sha256':POLICY_SHA256}
        identity = digest(encoded(value))
        path = f'{self.base}/{kind}/{identity}.json'
        target = self.ws.checked_path(path,create_parent=True)
        if target.exists():
            if self.ws.read(path) != encoded(value):
                raise BoundaryError('Immutable artifact conflict')
        else:
            self.ws.write(path,encoded(value))
        return {'id':identity,'path':path,**value}

    def _load(self, kind, identity):
        if len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity):
            raise BoundaryError('Invalid health artifact ID')
        path=f'{self.base}/{kind}/{identity}.json'
        value=self._read(path)
        if digest(encoded(value))!=identity or value['mode']!=self.mode or value['policy_sha256']!=POLICY_SHA256:
            raise BoundaryError('Artifact binding mismatch')
        return value

    def _provenance(self, refs):
        if not refs or len(refs)>20:
            raise BoundaryError('Bound evidence required')
        for ref in refs:
            path=ref['path']
            if not path.startswith(('data/','validation/','config/health/')) or '/secrets/' in '/'+path:
                raise BoundaryError('Only local tool receipt evidence allowed')
            raw=self.ws.read(path)
            if digest(raw)!=ref['sha256']:
                raise BoundaryError('Receipt hash mismatch')
            # Optional exact JSON binding avoids manual metric transcription drift.
            if path.endswith('.json') and self.mode=='REAL':
                document=json.loads(raw)
                if isinstance(document,dict) and (document.get('mode')=='FIXTURE' or document.get('classification') in ('FIXTURE','SYNTHETIC')):
                    raise BoundaryError('Fixture evidence cannot substantiate REAL health')
            if 'pointer' in ref:
                value=json.loads(raw)
                for key in ref['pointer']:
                    value=value[key]
                if value!=ref['value']:
                    raise BoundaryError('Receipt field mismatch')

    def import_signals(self, manifest_path):
        doc=self._read(manifest_path)
        if not isinstance(doc,dict) or not isinstance(doc.get('signals'),list) or doc.get('mode')!=self.mode or doc.get('classification')!='PRIVATE':
            raise BoundaryError('REAL/FIXTURE or privacy mismatch')
        # Validate whole batch before any mutation. A crash during writes is safe to retry.
        batch=[]
        for s in doc['signals']:
            if not isinstance(s,dict) or s.get('mode')!=self.mode or any(k not in s for k in (*STRATUM,'execution_id','occurred_at','success','independent_task','provenance','cause')):
                raise BoundaryError('Incomplete signal or mode mismatch')
            if not all(isinstance(s[k],str) for k in STRATUM) or not isinstance(s.get('metrics',{}),dict):
                raise BoundaryError('Invalid stratum or metrics')
            if not isinstance(s['execution_id'],str) or not s['execution_id']:
                raise BoundaryError('Stable execution identity required')
            if type(s['independent_task']) is not bool or (s['success'] is not None and type(s['success']) is not bool):
                raise BoundaryError('Outcome/independence must be explicit')
            if 'error_code' in s and s['cause']!=classify_observation(s['error_code']):
                raise BoundaryError('Failure surface classification mismatch')
            if s['cause'] not in CAUSES:
                raise BoundaryError('Unknown cause category')
            if s['occurred_at'] is not None:
                timestamp(s['occurred_at'])
            for value in s.get('metrics',{}).values():
                if value is not None and (type(value) not in (float,int) or not math.isfinite(value) or value<0):
                    raise BoundaryError('Invalid numeric metric')
            self._provenance(s['provenance'])
            s={**s,'classification':'PRIVATE','policy_sha256':POLICY_SHA256}
            identity=digest(s['execution_id'].encode())
            batch.append((identity,s))
        if len({i for i,s in batch})!=len(batch):
            raise BoundaryError('Duplicate execution in batch')
        imported=0
        with Store(self.ws):
            for identity,s in batch:
                path=f'{self.base}/signals/{identity}.json'
                target=self.ws.checked_path(path,create_parent=True)
                if target.exists() and self._read(path)!=s:
                    raise BoundaryError('Existing execution cannot be rewritten')
            for identity,s in batch:
                path=f'{self.base}/signals/{identity}.json'
                if not self.ws.checked_path(path,create_parent=True).exists():
                    self.ws.write(path,encoded(s));imported+=1
        return {'imported':imported,'duplicate_reused':len(batch)-imported,'mode':self.mode,'manifest_sha256':digest(self.ws.read(manifest_path))}

    def signals(self):
        directory=self.ws.root/self.base/'signals'
        results=[]
        if directory.exists():
            for p in sorted(directory.glob('*.json')):
                s=self._read(str(p.relative_to(self.ws.root)))
                if digest(s['execution_id'].encode())!=p.stem or s['mode']!=self.mode or s['policy_sha256']!=POLICY_SHA256:
                    raise BoundaryError('Signal binding mismatch')
                results.append(s)
        return results

    def inventory(self):
        groups=defaultdict(list)
        for s in self.signals():groups[stratum_id(s)].append(s)
        return {'mode':self.mode,'classification':'PRIVATE','policy_sha256':POLICY_SHA256,'total_executions':sum(map(len,groups.values())),
                'strata':[{'id':k,'key':{f:v[0][f] for f in STRATUM},**summary(v),
                           'eligible_n':sum(eligible(x) for x in v),'causes':dict(Counter(x['cause'] for x in v)),
                           'exclusions':[{'execution_id':x['execution_id'],'independent_task':x['independent_task'],
                                          'time_unknown':x['occurred_at'] is None,'unknown_fields':[f for f in STRATUM if x[f] in UNKNOWN]} for x in v if not eligible(x)]} for k,v in sorted(groups.items())]}

    def baseline(self):
        with Store(self.ws):
            groups=defaultdict(list)
            for s in self.signals():groups[stratum_id(s)].append(s)
            strata=[]
            for identity, signals in sorted(groups.items()):
                valid=sorted((s for s in signals if eligible(s)),key=lambda s:(timestamp(s['occurred_at']),s['execution_id']))
                selected=valid[:self.policy['baseline_min']]
                strata.append({'id':identity,'key':{f:signals[0][f] for f in STRATUM},'status':'READY' if len(selected)>=self.policy['baseline_min'] else 'INSUFFICIENT_DATA',
                               'available_n':len(signals),'eligible_n':len(valid),'missing_baseline_n':max(0,50-len(selected)),
                               'sample_ids':[s['execution_id'] for s in selected],'sample_hashes':[digest(encoded(s)) for s in selected],
                               'cutoff':max((s['occurred_at'] for s in selected),key=timestamp,default=None),**summary(selected)})
            return self._put('baselines',{'kind':'HEALTH_BASELINE','policy_version':self.policy['version'],'created_at':now(),
                                         'status':'READY' if any(s['status']=='READY' for s in strata) else 'INSUFFICIENT_DATA','strata':strata})

    def diagnose(self, path):
        doc=self._read(path)
        if not isinstance(doc,dict) or not all(k in doc for k in ('assessment_id','stratum_id','provenance')) or doc.get('mode')!=self.mode or doc.get('cause') not in CAUSES or doc.get('confidence') not in ('HIGH','MEDIUM','LOW','UNKNOWN'):
            raise BoundaryError('Invalid diagnosis')
        assessment=self._load('assessments',doc['assessment_id'])
        if doc['stratum_id'] not in [s['id'] for s in assessment['strata']]:
            raise BoundaryError('Diagnosis stratum mismatch')
        self._provenance(doc['provenance'])
        exclusions=doc.get('exclusions',{})
        if not set(exclusions)<=set(self.policy['required_exclusions']):
            raise BoundaryError('Unknown exclusion')
        for item in exclusions.values():
            if item['status'] not in ('RULED_OUT','PRESENT','UNKNOWN'):
                raise BoundaryError('Invalid exclusion')
            self._provenance(item['provenance'])
        # Preserve submitted author/source, not an invented human identity.
        if not doc.get('recorded_by') or not doc.get('reason'):
            raise BoundaryError('Diagnosis attribution required')
        with Store(self.ws):
            return self._put('diagnoses',{**doc,'recorded_at':now(),'authority':'INTERNAL_DIAGNOSIS_NOT_PRODUCTION_CONTROL'})

    def assess(self, baseline_id):
        baseline=self._load('baselines',baseline_id)
        signals=self.signals();by_id={s['execution_id']:s for s in signals};rows=[]
        for b in baseline['strata']:
            for identity,sha in zip(b['sample_ids'],b['sample_hashes']):
                if identity not in by_id or digest(encoded(by_id[identity]))!=sha:
                    raise BoundaryError('Versioned baseline sample changed')
            future=sorted((s for s in signals if stratum_id(s)==b['id'] and eligible(s) and s['execution_id'] not in b['sample_ids'] and b['cutoff'] and timestamp(s['occurred_at'])>timestamp(b['cutoff'])),key=lambda s:(timestamp(s['occurred_at']),s['execution_id']))
            # Version/evaluator/task changes never enter this stratum's trend.
            drift=[s['execution_id'] for s in signals if s['capability']==b['key']['capability'] and stratum_id(s)!=b['id'] and s['occurred_at'] and b['cutoff'] and timestamp(s['occurred_at'])>timestamp(b['cutoff'])]
            windows=[future[i:i+20] for i in range(0,len(future)-19,20)][-3:]
            stats=[{**summary(w),'sample_ids':[s['execution_id'] for s in w]} for w in windows]
            sustained=b['status']=='READY' and len(stats)==3 and all(w['success_wilson95'][1]<b['success_wilson95'][0] for w in stats)
            status='INSUFFICIENT_DATA' if b['status']!='READY' or len(stats)<3 else ('WARNING' if sustained else 'NO_SUSTAINED_EXECUTION_DECLINE_DETECTED')
            rows.append({'id':b['id'],'key':b['key'],'status':status,'sustained_execution_decline':sustained,'windows':stats,
                         'future_eligible_n':len(future),'incomplete_tail_n':len(future)%20,'missing_trend_n':max(0,60-len(future)),
                         'noncomparable_future_ids':drift,'root_cause':'UNKNOWN','quality':'NOT_INFERRED','latency_cost':'DESCRIPTIVE_ONLY'})
        with Store(self.ws):
            return self._put('assessments',{'kind':'HEALTH_ASSESSMENT','baseline_id':baseline_id,'created_at':now(),'strata':rows,
                                          'status':'INSUFFICIENT_DATA' if not any(r['status']!='INSUFFICIENT_DATA' for r in rows) else 'ASSESSED','renewal_generated':False})

    def control(self, enabled, reason):
        if type(enabled) is not bool or not reason:
            raise BoundaryError('Explicit control and reason required')
        with Store(self.ws):
            event=self._put('controls',{'renewal_enabled':enabled,'reason':reason,'at':now(),'preserve_signals_and_diagnoses':True})
            self.ws.write(f'{self.base}/control.json',encoded({'event_id':event['id']}),replace=True)
            return event

    def renewal(self, assessment_id, diagnosis_id):
        with Store(self.ws):
            assessment=self._load('assessments',assessment_id);diagnosis=self._load('diagnoses',diagnosis_id)
            control_path=f'{self.base}/control.json'
            enabled=False
            if (self.ws.root/control_path).exists():
                enabled=self._load('controls',self._read(control_path)['event_id'])['renewal_enabled']
            rows={r['id']:r for r in assessment['strata']}
            row=rows.get(diagnosis['stratum_id'],{})
            reasons=[]
            if not enabled:reasons.append('TRIGGER_DISABLED')
            if diagnosis['assessment_id']!=assessment_id:reasons.append('ASSESSMENT_MISMATCH')
            if not row.get('sustained_execution_decline'):reasons.append('NO_QUALIFIED_SUSTAINED_DECLINE')
            if diagnosis['cause']!='MODULE / IMPLEMENTATION' or diagnosis.get('module_finding') not in self.policy['renewal_causes'] or diagnosis['confidence']!='HIGH':reasons.append('ROOT_CAUSE_NOT_SUFFICIENT')
            if any(diagnosis.get('exclusions',{}).get(k,{}).get('status')!='RULED_OUT' for k in self.policy['required_exclusions']):reasons.append('ALTERNATIVE_CAUSES_NOT_RULED_OUT')
            if row.get('noncomparable_future_ids'):reasons.append('COMPARABILITY_REVIEW_REQUIRED')
            if reasons:return {'status':'NOT_TRIGGERED','reasons':reasons,'mode':self.mode,'production_actions':[]}
            # Deterministic artifact: duplicate gate invocation does not create another RQ.
            return self._put('renewal_questions',{'kind':'RENEWAL_RESEARCH_QUESTION','authority':'INTERNAL_RESEARCH_ONLY',
                'assessment_id':assessment_id,'diagnosis_id':diagnosis_id,'capability':row['key']['capability'],
                'question':'Which change could address the evidence-backed '+diagnosis['module_finding']+' in '+row['key']['capability']+'?',
                'status':'RESEARCH_PROPOSED','health_status':'DEGRADED','production_actions':[],
                'discovery_started':False,'provider_change':False,'EXPERIMENT_started':False})

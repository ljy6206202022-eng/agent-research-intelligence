"""Default-disabled RD-5 mechanics. Fixtures cannot promote real providers.

There is deliberately no command execution, deployment API or Project writer here.
Only pre-recorded, hash-bound evaluation outputs can be compared.
"""
import hashlib
import json
from uuid import uuid4
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical


class Experiments:
    def __init__(self,workspace):self.ws=workspace
    def _save(self,kind,record):
        record={**record,'classification':'FIXTURE','EXPERIMENT_ACCEPTED':False,'production_effects':[],
                'provider_replacement':False,'id':uuid4().hex}
        path='data/experiments/fixture/'+record['id']+'/'+kind+'.json'
        self.ws.write(path,canonical(record));return {**record,'artifact':path}
    def _fixture(self,request):
        if request.get('mode')!='FIXTURE' or request.get('fixture_enabled') is not True:
            raise BoundaryError('RD5_DISABLED_REAL_REPLACEMENT_REQUIRES_HEALTH_AND_SEPARATE_USER_REVIEW')
    def _read(self,binding):
        if not binding['artifact'].startswith(('validation/','data/experiments/fixture/')):
            raise BoundaryError('Fixture experiment namespace required')
        raw=self.ws.read(binding['artifact'])
        if hashlib.sha256(raw).hexdigest()!=binding['sha256']:raise BoundaryError('Experiment input hash mismatch')
        value=json.loads(raw)
        if value.get('classification')!='FIXTURE':raise BoundaryError('Fixture provenance required')
        return value
    def run(self,request):
        self._fixture(request);old=self._read(request['baseline']);new=self._read(request['candidate'])
        if old['task_ids']!=new['task_ids'] or old['evaluation_policy']!=new['evaluation_policy']:
            raise BoundaryError('Comparable task identity and evaluator required')
        keys=('quality','cost','tokens','latency','failure_rate','compatibility','state_preservation','permissions')
        comparison={k:{'baseline':old.get(k),'candidate':new.get(k),'status':'UNKNOWN' if k not in old or k not in new else 'OBSERVED'} for k in keys}
        return self._save('experiment',{'operation':'ExperimentRun','inputs':request,'comparison':comparison,
            'decision':'REVIEW_REQUIRED','models_executed':0,'meaning':'Offline evaluation of supplied fixture outcomes, not a real experiment'})
    def shadow(self,request):
        self._fixture(request);old=self._read(request['baseline']);new=self._read(request['candidate'])
        if old['task_ids']!=new['task_ids']:raise BoundaryError('Shadow task mismatch')
        return self._save('shadow',{'operation':'ShadowRun','inputs':request,'authoritative_output':old,
            'candidate_output':new,'candidate_controls_state':False,'production_shadow_run':False})
    def canary(self,request):
        self._fixture(request)
        if not request.get('review_basis') or request.get('compatibility_pass') is not True:
            raise BoundaryError('Explicit fixture review and compatibility required')
        previous=self._read(request['previous']) if request.get('previous') else {'percentage':0,'authoritative_provider':request['baseline_provider']}
        allowed={0:10,10:30,30:100}
        if allowed.get(previous['percentage'])!=request['percentage']:raise BoundaryError('Canary steps are 10, 30, 100 without skipping')
        if previous['percentage'] and any(previous[k]!=request[k] for k in ('candidate_provider','original_config','candidate_config')):
            raise BoundaryError('Canary candidate and rollback baseline must remain fixed')
        result={'operation':'CanaryPromote','percentage':request['percentage'],'authoritative_provider':previous['authoritative_provider'],
            'candidate_provider':request['candidate_provider'],'previous':request.get('previous'),
            'compatibility_pass':True,'review_basis':request['review_basis'],
            'original_config':request['original_config'],'candidate_config':request['candidate_config'],
            'migration_log':request.get('migration_log',[]),'routing_simulated_only':True}
        return self._save('canary',result)
    def rollback(self,request):
        self._fixture(request);state=self._read(request['canary'])
        if state.get('operation')!='CanaryPromote':raise BoundaryError('Canary record required')
        return self._save('rollback',{'operation':'Rollback','restored_fixture_provider':state['authoritative_provider'],
            'restored_fixture_config':state['original_config'],'previous_canary':request['canary'],
            'migration_log':state['migration_log'],'reason':request['reason'],
            'real_provider_changed':False,'meaning':'Internal fixture rollback record; no runtime replacement was started'})

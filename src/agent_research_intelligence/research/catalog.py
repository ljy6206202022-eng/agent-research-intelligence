"""Versioned Versioned v0.1 records over the existing audited, private Store.

Source state is an observation, never an account mutation or truth promotion.
Legacy artifacts remain immutable and are referenced rather than rewritten.
"""
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.storage.database import Store
from agent_research_intelligence.storage.models import ResearchOutput


def now():
    return datetime.now(timezone.utc).isoformat()


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)


class Basis(Record):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    locator: str = Field(min_length=1)
    classification: Literal['REAL', 'REUSED_REAL', 'FIXTURE', 'USER_INPUT']


class Question(Record):
    problem: str = Field(min_length=10, max_length=20000)
    current_context: str
    constraints: list[str]
    need_to_learn: list[str] = Field(min_length=1)
    search_terms: list[str] = Field(min_length=1, max_length=20)
    related_capabilities: list[str] = []
    risk: str = 'INTERNAL_RESEARCH_ONLY'
    status: Literal['OPEN', 'RESEARCHING', 'DOSSIER_READY', 'WATCHING', 'DORMANT', 'REOPENED'] = 'OPEN'
    context_basis: list[Basis] = []


class Source(Record):
    url: str = Field(pattern=r'^https://')
    entity_name: str
    platform: str
    kind: Literal['channel', 'video', 'github', 'paper', 'engineering_blog', 'research_lab', 'standard', 'feed', 'web', 'creator']
    platform_id: str | None = None
    creator_id: str | None = None
    identity_candidates: list[dict] = []
    topics: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    historical_utility: dict | None = None
    validation_count: int | None = Field(default=None, ge=0)
    density_assessment: dict | None = None
    status: Literal['DISCOVERED', 'CANDIDATE', 'VALIDATED', 'SUBSCRIBED', 'WATCHED', 'ACTIVE', 'DEGRADED', 'TRUSTED_FOR_TOPIC', 'ARCHIVED'] = 'DISCOVERED'
    subscription_state: Literal['UNKNOWN', 'NOT_SUBSCRIBED', 'SUBSCRIBED', 'RECOMMEND_ONLY'] = 'UNKNOWN'
    trust_level: Literal['UNKNOWN', 'OBSERVED', 'TRUSTED_FOR_TOPIC'] = 'UNKNOWN'
    source_provenance: Literal['AUTHENTICATED_SUBSCRIPTION_READ', 'USER_ADDED', 'TOOL_DISCOVERY', 'HISTORICAL_REGISTRY', 'DEVELOPMENT_SEED']
    basis: list[Basis] = Field(min_length=1)
    limitations: list[str] = []


class Evidence(Record):
    rq_id: str
    source_id: str
    source_revision: int = Field(ge=1)
    published_at: str | None
    captured_at: str
    claim: str = Field(min_length=1)
    locator: dict = Field(min_length=1)
    evidence_type: Literal['SOURCE_ASSERTION', 'CODE', 'PAPER', 'STANDARD', 'TRANSCRIPT', 'FRAME', 'EXPERIMENT']
    transcript_source: Literal['manual_caption', 'auto_caption', 'external_caption', 'asr_generated'] | None = None
    source_quality: dict
    relevance: dict
    novelty: dict
    impact: dict
    confidence: Literal['HIGH', 'MEDIUM', 'LOW', 'UNKNOWN']
    limitations: list[str] = Field(min_length=1)
    provenance: list[Basis] = Field(min_length=1)
    classification: Literal['REAL', 'REUSED_REAL', 'FIXTURE']
    human_verified: bool = False

    @model_validator(mode='after')
    def verify_classification(self):
        if self.classification != 'FIXTURE' and any(b.classification == 'FIXTURE' for b in self.provenance):
            raise ValueError('Fixture cannot become real evidence')
        if self.human_verified and not any(b.classification == 'USER_INPUT' for b in self.provenance):
            raise ValueError('Human verification requires actual review provenance')
        return self


TRANSITIONS = {
    'DISCOVERED': {'CANDIDATE', 'ARCHIVED'}, 'CANDIDATE': {'VALIDATED', 'ARCHIVED'},
    'VALIDATED': {'SUBSCRIBED', 'WATCHED', 'ARCHIVED'}, 'SUBSCRIBED': {'ACTIVE', 'ARCHIVED'},
    'WATCHED': {'ACTIVE', 'ARCHIVED'}, 'ACTIVE': {'DEGRADED', 'TRUSTED_FOR_TOPIC', 'ARCHIVED'},
    'DEGRADED': {'ACTIVE', 'ARCHIVED'}, 'TRUSTED_FOR_TOPIC': {'DEGRADED', 'ARCHIVED'},
    'ARCHIVED': {'CANDIDATE'},
}
MODELS = {'question': Question, 'source': Source, 'evidence': Evidence}


class Catalog:
    def __init__(self, workspace):
        self.ws = workspace

    def validate_basis(self, bases):
        for b in bases:
            if not b.artifact.startswith(('data/', 'validation/', 'docs/', 'config/permissions/')):
                raise BoundaryError('Evidence must refer to owned artifacts, never credentials')
            if hashlib.sha256(self.ws.read(b.artifact, limit=32*1024**2)).hexdigest() != b.sha256:
                raise BoundaryError('Evidence artifact hash mismatch')

    @staticmethod
    def _schema(store):
        store.db.executescript('''
        CREATE TABLE IF NOT EXISTS frozen_catalog (
          id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
          record_id TEXT NOT NULL REFERENCES records(id), PRIMARY KEY(id,revision));
        CREATE TRIGGER IF NOT EXISTS frozen_catalog_no_update BEFORE UPDATE ON frozen_catalog
          BEGIN SELECT RAISE(ABORT,'immutable catalog revision'); END;
        CREATE TRIGGER IF NOT EXISTS frozen_catalog_no_delete BEFORE DELETE ON frozen_catalog
          BEGIN SELECT RAISE(ABORT,'immutable catalog revision'); END;
        ''')

    def read(self, identifier, revision=None):
        with Store(self.ws) as s:
            self._schema(s)
            row = s.db.execute('SELECT record_id FROM frozen_catalog WHERE id=? '+
                               ('AND revision=?' if revision is not None else 'ORDER BY revision DESC LIMIT 1'),
                               (identifier, revision) if revision is not None else (identifier,)).fetchone()
            if not row:
                raise BoundaryError('Catalog record not found')
            return json.loads(s.get(row[0]).text)

    def search(self, kind, terms=()):
        if kind not in {*MODELS, 'dossier', 'mapping', 'conflict', 'score', 'audit', 'experiment'}:
            raise BoundaryError('Unknown catalog kind')
        with Store(self.ws) as s:
            self._schema(s)
            rows = s.db.execute('''SELECT r.payload FROM frozen_catalog c JOIN records r ON c.record_id=r.id
                WHERE c.kind=? AND c.revision=(SELECT max(d.revision) FROM frozen_catalog d WHERE d.id=c.id)''', (kind,)).fetchall()
            results = [json.loads(json.loads(row[0])['text']) for row in rows]
        return [r for r in results if not terms or any(t.casefold() in json.dumps(r['payload'], ensure_ascii=False).casefold() for t in terms)]

    def put(self, kind, payload, *, identifier=None, expected_revision=0, reason):
        if not reason or len(reason)>4000:
            raise BoundaryError('Explicit revision reason required')
        if kind not in {*MODELS, 'dossier', 'mapping', 'conflict', 'score', 'audit', 'experiment'}:
            raise BoundaryError('Unknown catalog kind')
        if kind in MODELS:
            model = MODELS[kind].model_validate(payload)
            for field in ('basis', 'context_basis', 'provenance'):
                self.validate_basis(getattr(model, field, []))
            payload = model.model_dump(mode='json')
            if kind=='source':
                for candidate in payload['identity_candidates']:
                    if not candidate.get('name') or not candidate.get('basis'):raise BoundaryError('Identity candidates require names and locators')
                    self.validate_basis([Basis.model_validate(b) for b in candidate['basis']])
        identifier = identifier or uuid4().hex
        if not identifier.isalnum() or len(identifier)>64:
            raise BoundaryError('Invalid catalog id')
        with Store(self.ws) as s:
            self._schema(s)
            old = s.db.execute('SELECT revision,kind,record_id FROM frozen_catalog WHERE id=? ORDER BY revision DESC LIMIT 1', (identifier,)).fetchone()
            revision = old[0] if old else 0
            if revision != expected_revision or old and old[1] != kind:
                raise BoundaryError('Revision conflict')
            if kind == 'source':
                if payload['creator_id']:
                    creator=s.db.execute('SELECT record_id FROM frozen_catalog WHERE id=? AND kind=? ORDER BY revision DESC LIMIT 1',
                                         (payload['creator_id'],'source')).fetchone()
                    if not creator or json.loads(s.get(creator[0]).text)['payload']['kind']!='creator':
                        raise BoundaryError('Creator association requires a located creator profile')
                if old:
                    previous = json.loads(s.get(old[2]).text)['payload']
                    if previous['status'] != payload['status'] and payload['status'] not in TRANSITIONS[previous['status']]:
                        raise BoundaryError('Invalid source lifecycle transition')
                    if previous['url'] != payload['url'] or previous['platform_id'] != payload['platform_id']:
                        raise BoundaryError('Source identity cannot change')
                    if previous != payload and previous['basis'] == payload['basis']:
                        raise BoundaryError('Source update requires new observed/reviewed basis')
                elif payload['status'] not in ('DISCOVERED', 'CANDIDATE'):
                    raise BoundaryError('New sources start DISCOVERED or CANDIDATE')
                if payload['subscription_state']=='SUBSCRIBED' and payload['source_provenance'] != 'AUTHENTICATED_SUBSCRIPTION_READ':
                    raise BoundaryError('Subscribed state requires authenticated read provenance')
                if payload['status']=='SUBSCRIBED' and payload['subscription_state']!='SUBSCRIBED':
                    raise BoundaryError('SUBSCRIBED lifecycle requires observed subscription')
            if kind == 'evidence':
                for rid, typ, rev in [(payload['rq_id'],'question',None), (payload['source_id'],'source',payload['source_revision'])]:
                    query='SELECT 1 FROM frozen_catalog WHERE id=? AND kind=?'
                    params=[rid,typ]
                    if rev is not None:query+=' AND revision=?';params.append(rev)
                    if not s.db.execute(query,params).fetchone():raise BoundaryError('Evidence RQ/source binding missing')
            entry={'schema':'frozen-v0.1-catalog-1','id':identifier,'revision':revision+1,'kind':kind,
                   'at':now(),'reason':reason,'payload':payload,'authority':'INTERNAL_RESEARCH_ONLY'}
            record=ResearchOutput(kind='evidence' if kind=='evidence' else 'dossier' if kind=='dossier' else 'proposal',
                                  classification='PRIVATE',text=canonical(entry).decode())
            record_id=uuid4().hex
            with s.db:
                s.db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)',(record_id,record.kind,record.classification,
                    record.authority,record.model_dump_json(),now()))
                s._audit('record_created',record_id,'PERSISTED_INTERNAL_ONLY',hashlib.sha256(record.model_dump_json().encode()).hexdigest())
                s.db.execute('INSERT INTO frozen_catalog VALUES(?,?,?,?)',(identifier,revision+1,kind,record_id))
            return entry

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from agent_research_intelligence.governance.paths import BoundaryError, Workspace
from agent_research_intelligence.governance.policy import POLICY_HASH, canonical, verify_policy
from .models import ResearchOutput

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE records (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('evidence','dossier','proposal','private_snapshot')),
 classification TEXT NOT NULL CHECK(classification IN ('PUBLIC','PRIVATE')),
 authority TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 CHECK((kind='evidence' AND authority='EXTERNAL_EVIDENCE') OR
       (kind='dossier' AND authority='RESEARCH_OUTPUT') OR
       (kind='proposal' AND authority='PROPOSAL_ONLY') OR
       (kind='private_snapshot' AND authority='READ_ONLY_PROJECT_MATERIAL' AND classification='PRIVATE'))
);
CREATE TABLE audit (
 sequence INTEGER PRIMARY KEY, previous_hash TEXT NOT NULL, event TEXT NOT NULL, hash TEXT UNIQUE NOT NULL
);
CREATE TRIGGER immutable_records_update BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER immutable_records_delete BEFORE DELETE ON records BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER immutable_audit_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER immutable_audit_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
PRAGMA user_version=1;
"""


class Store:
    """Single process lease. Audit and object writes share one transaction."""
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        verify_policy(workspace)
        self.db = None
        self.lease = None
        try:
            lock = workspace.checked_path("data/db/lease.lock", create_parent=True)
            self.lease = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if os.fstat(self.lease).st_nlink != 1:
                raise BoundaryError("Hardlinked lease denied")
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for name in ("research.sqlite3", "research.sqlite3-journal", "research.sqlite3-wal", "research.sqlite3-shm"):
                workspace.checked_path("data/db/" + name)
            path = workspace.checked_path("data/db/research.sqlite3")
            if not path.exists():
                workspace.write("data/db/research.sqlite3", b"")
            self.db = sqlite3.connect(path, timeout=2)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if self.db.execute("SELECT name FROM sqlite_master").fetchone():
                    raise BoundaryError("Unknown existing database")
                self.db.executescript(SCHEMA)
            elif version != SCHEMA_VERSION:
                raise BoundaryError("Incompatible schema; no automatic migration")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BoundaryError("Database integrity failure")
            self.verify_audit()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _audit(self, action: str, object_id: str, effect: str, payload_sha256: str | None = None) -> None:
        previous = self.db.execute("SELECT hash FROM audit ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        event = canonical({"actor": "local_operator", "action": action, "object_id": object_id,
                           "effect": effect, "policy_hash": POLICY_HASH,
                           "payload_sha256": payload_sha256,
                           "time": datetime.now(timezone.utc).isoformat()})
        digest = hashlib.sha256(previous_hash.encode() + event).hexdigest()
        self.db.execute("INSERT INTO audit(previous_hash,event,hash) VALUES(?,?,?)", (previous_hash, event.decode(), digest))

    def append(self, record: ResearchOutput) -> str:
        if record.kind == "private_snapshot" and record.classification != "PRIVATE":
            raise BoundaryError("Project snapshots must remain private")
        identifier = uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO records VALUES(?,?,?,?,?,?)", (
                identifier, record.kind, record.classification, record.authority,
                record.model_dump_json(), datetime.now(timezone.utc).isoformat()))
            self._audit("record_created", identifier, "PERSISTED_INTERNAL_ONLY",
                        hashlib.sha256(record.model_dump_json().encode()).hexdigest())
        return identifier

    def append_once(self, identifier: str, record: ResearchOutput) -> str:
        """Deterministic event identity, transactionally audited; conflicting replay fails."""
        if len(identifier)!=64 or any(c not in '0123456789abcdef' for c in identifier):
            raise BoundaryError('Invalid deterministic record ID')
        payload=record.model_dump_json()
        with self.db:
            old=self.db.execute('SELECT payload FROM records WHERE id=?',(identifier,)).fetchone()
            if old:
                if old[0]!=payload:raise BoundaryError('Idempotency payload conflict')
                return identifier
            self.db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)',(identifier,record.kind,record.classification,
                record.authority,payload,datetime.now(timezone.utc).isoformat()))
            self._audit('record_created',identifier,'PERSISTED_INTERNAL_ONLY',hashlib.sha256(payload.encode()).hexdigest())
        return identifier

    def get(self, identifier: str) -> ResearchOutput:
        row = self.db.execute("SELECT payload FROM records WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return ResearchOutput.model_validate_json(row[0])

    def verify_audit(self) -> int:
        previous, count = "0" * 64, 0
        recorded_ids = set()
        for prev, event, digest in self.db.execute("SELECT previous_hash,event,hash FROM audit ORDER BY sequence"):
            if prev != previous or hashlib.sha256(prev.encode() + event.encode()).hexdigest() != digest:
                raise BoundaryError("Audit chain integrity failure")
            value = json.loads(event)
            if value["action"] == "record_created":
                row = self.db.execute("SELECT kind,classification,authority,payload FROM records WHERE id=?", (value["object_id"],)).fetchone()
                if row is None or hashlib.sha256(row[3].encode()).hexdigest() != value["payload_sha256"]:
                    raise BoundaryError("Audit-to-record binding failure")
                record = ResearchOutput.model_validate_json(row[3])
                if tuple(row[:3]) != (record.kind, record.classification, record.authority):
                    raise BoundaryError("Record authority mismatch")
                recorded_ids.add(value["object_id"])
            previous, count = digest, count + 1
        if recorded_ids != {row[0] for row in self.db.execute("SELECT id FROM records")}:
            raise BoundaryError("Records missing audit events")
        return count

    def snapshot(self) -> dict:
        if self.db.in_transaction:
            raise BoundaryError("End the active transaction before starting a backup")
        relative = f"backups/{uuid4().hex}/research.sqlite3"
        backup_root = relative.rsplit("/", 1)[0]
        self.workspace.write(relative, b"")
        with sqlite3.connect(self.workspace.checked_path(relative)) as target:
            self.db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BoundaryError("Backup verification failed")
        digest = hashlib.sha256(self.workspace.read(relative, limit=128 * 1024 * 1024)).hexdigest()
        artifacts = []
        copied = set()
        def copy_artifact(source):
            if source in copied:
                return
            data = self.workspace.read(source)
            destination = backup_root + "/" + source
            self.workspace.write(destination, data)
            artifacts.append({"restore_path": source, "backup_path": destination,
                              "sha256": hashlib.sha256(data).hexdigest()})
            copied.add(source)
        for (payload,) in self.db.execute("SELECT payload FROM records WHERE kind='private_snapshot'"):
            record = ResearchOutput.model_validate_json(payload)
            for prefix in record.source_refs:
                if not prefix.startswith("data/private_context/"):
                    raise BoundaryError("Invalid snapshot artifact reference")
                for suffix in (".content", ".json"):
                    source = prefix + suffix
                    copy_artifact(source)
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        research_schema = None
        if 'research_schema' in tables:
            research_schema = self.db.execute("SELECT version FROM research_schema").fetchone()[0]
            question_ids = {row[0] for row in self.db.execute("SELECT id FROM research_questions")}
            for question_id in question_ids:
                if len(question_id) != 32 or any(c not in '0123456789abcdef' for c in question_id):
                    raise BoundaryError("Invalid research artifact directory")
                directory = self.workspace.root / 'data/research' / question_id
                if directory.is_symlink():
                    raise BoundaryError("Research artifact symlink denied")
                for path in directory.iterdir():
                    copy_artifact(str(path.relative_to(self.workspace.root)))
        manifest = {"database": relative, "sha256": digest, "schema_version": SCHEMA_VERSION,
                    "policy_hash": POLICY_HASH, "scope": "RD_TOOL_ONLY", "includes_credentials": False,
                    "artifacts": artifacts, "research_schema_version": research_schema}
        # discovery uses the existing immutable records schema and an additive artifact tree.
        # Include those source/evidence snapshots so a restored Dossier has its locators.
        for folder in ('data/discovery','data/sources','data/browser','data/artifacts','data/watch','data/health','data/catalog','data/youtube','data/experiments'):
            directory=self.workspace.root/folder
            if directory.is_symlink():raise BoundaryError('discovery backup symlink denied')
            if not directory.exists():continue
            for path in directory.rglob('*'):
                if path.is_symlink():raise BoundaryError('discovery backup symlink denied')
                if path.is_file():copy_artifact(str(path.relative_to(self.workspace.root)))
        manifest['discovery_artifact_trees_included']=True
        self.workspace.write(relative + ".manifest.json", canonical(manifest))
        with self.db:
            self._audit("backup_created", relative, "BACKUP_INTEGRITY_VERIFIED")
        return manifest

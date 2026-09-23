"""source vertical slice: explicit question/source, traceable excerpts, local dossier.

No cloud model is enabled. Synthesis and architecture recommendations remain
human-reviewed; an extractive dossier is not an accepted technical decision.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent_research_intelligence.acquisition.youtube import YouTube, description_links, video_id
from agent_research_intelligence.connectors.github import GitHub
from agent_research_intelligence.connectors.public_http import AcquisitionError, PublicHTTP
from agent_research_intelligence.governance.paths import BoundaryError, Workspace
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.governance.project_reader import PROJECT_STATE, read_project_file
from agent_research_intelligence.storage.database import Store
from agent_research_intelligence.storage.models import ResearchOutput


class ResearchQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    original: str = Field(min_length=10, max_length=20000)
    constraints: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    source_url: str


def _safe_markdown(text: str) -> str:
    # External content cannot inject HTML, images, or executable link schemes.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("[", "\\[").replace("]", "\\]")


class ResearchService:
    def __init__(self, workspace: Workspace, *, http=None):
        self.workspace = workspace
        self.http = http or PublicHTTP()

    def _save(self, relative, value):
        self.workspace.write(relative, canonical(value))
        return relative

    def _initialize_registry(self, store):
        tables = {x[0] for x in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'research_schema' in tables:
            if store.db.execute("SELECT version FROM research_schema").fetchall() != [(1,)]:
                raise BoundaryError("Unsupported research registry schema")
            return
        if tables & {'research_questions', 'research_sources'}:
            raise BoundaryError("Unversioned research registry; operator review required")
        # Explicit first-use initialization, backed up before the new schema.
        store.snapshot()
        store.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE research_schema (version INTEGER PRIMARY KEY CHECK(version=1));
        INSERT INTO research_schema VALUES(1);
        CREATE TABLE research_questions (id TEXT NOT NULL, version INTEGER NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('RESEARCHING','DOSSIER_READY','BLOCKED')),
          artifact TEXT NOT NULL, PRIMARY KEY(id,version));
        CREATE TABLE research_sources (id TEXT PRIMARY KEY, url TEXT NOT NULL,
          channel_id TEXT NOT NULL, lifecycle TEXT NOT NULL CHECK(lifecycle='CANDIDATE'),
          subscription_state TEXT NOT NULL CHECK(subscription_state='NOT_REQUESTED'));
        CREATE TRIGGER questions_immutable_update BEFORE UPDATE ON research_questions
          BEGIN SELECT RAISE(ABORT,'immutable research question version'); END;
        CREATE TRIGGER questions_immutable_delete BEFORE DELETE ON research_questions
          BEGIN SELECT RAISE(ABORT,'immutable research question version'); END;
        COMMIT;
        """)

    def run(self, question: ResearchQuestion) -> dict:
        identifier = video_id(question.source_url)
        question_id = uuid4().hex
        prefix = f"data/research/{question_id}"
        with Store(self.workspace) as store:
            self._initialize_registry(store)
            intake = {"id": question_id, "version": 1, "classification": "PRIVATE", "origin": "USER_INPUT",
                      "question": question.model_dump(), "authority": "RESEARCH_INPUT_ONLY",
                      "need_to_learn": ["Locate evidence relevant to the original goal and constraints"],
                      "risk": "RESEARCH_ONLY_NO_PRODUCTION_AUTHORIZATION"}
            path = self._save(prefix + "/question-v1.json", intake)
            with store.db:
                store.db.execute("INSERT INTO research_questions VALUES(?,?,?,?)", (question_id, 1, "RESEARCHING", path))
                store._audit('question_created', question_id, 'RESEARCH_INPUT_ONLY')
            try:
                context_raw, context_receipt = read_project_file(PROJECT_STATE)
                project = json.loads(context_raw)
                context = {"classification": "PRIVATE", "source_receipt": context_receipt,
                           "canonical_revision": project.get('version', {}).get('revision'),
                           "stage_anchor": project.get('current', {}).get('stage_anchor'),
                           "capability_owner": "NOT_ESTABLISHED_BY_THIS_READ",
                           "permission_change": "NONE", "memory_write": "NONE"}
                self._save(prefix + '/local-context.json', context)
                provider = YouTube(self.http)
                try:
                    video = provider.fetch(question.source_url)
                finally:
                    diagnostics = self.http.diagnostics() if hasattr(self.http, 'diagnostics') else {"transport": getattr(self.http, 'trace', [])}
                    self._save(prefix + '/acquisition-trace.json', {"authority": "EXTERNAL_EVIDENCE", "stages": provider.trace, **diagnostics})
                metadata_path = self._save(prefix + "/video.json", video.model_dump())
                caption_ext = 'json' if provider.caption_body.lstrip().startswith(b'{') else 'xml'
                self.workspace.write(prefix + '/caption-source.' + caption_ext, provider.caption_body)
                urls = description_links(video.description, video.segments)
                with store.db:
                    store.db.execute("INSERT OR IGNORE INTO research_sources VALUES(?,?,?,?,?)", (identifier, question.source_url, video.channel_id, "CANDIDATE", "NOT_REQUESTED"))
                terms = [x.lower() for x in question.search_terms if x.strip()]
                if not terms:
                    terms = list(dict.fromkeys(re.findall(r"[a-zA-Z]{3,}", question.original.lower())))
                selected = [s for s in video.segments if any(t in s.text.lower() for t in terms)][:8] if terms else []
                # Never label the opening paragraphs relevant when no match exists.
                evidence = []
                for segment in selected:
                    locator = f"https://www.youtube.com/watch?v={identifier}&t={int(segment.start)}s"
                    evidence.append({"type": "TRANSCRIPT_EXCERPT", "claim": segment.text, "locator": locator,
                                     "start": segment.start, "end": segment.start + segment.duration,
                                     "speaker_id": segment.speaker_id, "speaker_name": None,
                                     "source_artifact": metadata_path,
                                     "source_sha256": hashlib.sha256(self.workspace.read(metadata_path)).hexdigest(),
                                     "confidence": "UNASSESSED", "limitations": "Source assertion; not independently established truth"})
                github_attempts = 0
                for link in urls["links"]:
                    if link["category"] != "github":
                        link["handoff"] = "CONNECTOR_NOT_ENABLED_IN_M1"
                        continue
                    if github_attempts >= 3:
                        link["handoff"] = "TASK_BUDGET_LIMIT"
                        continue
                    github_attempts += 1
                    try:
                        github = GitHub(self.http).fetch(link["url"])
                        artifact = self._save(prefix + "/github-" + uuid4().hex + ".json", github)
                        link.update(handoff="VERIFIED_README", artifact=artifact, commit=github["commit"])
                        lines = github["readme"].splitlines()
                        matches = [(i + 1, line) for i, line in enumerate(lines) if any(t in line.lower() for t in terms) and line.strip()][:4]
                        for line_number, line in matches:
                            evidence.append({"type": "README_EXCERPT", "claim": line, "locator": github["locator"] + f"#L{line_number}",
                                             "source_artifact": artifact, "source_sha256": hashlib.sha256(self.workspace.read(artifact)).hexdigest(),
                                             "description_url": link["url"], "description_offset": link["description_offset"],
                                             "spoken_references": link.get("spoken_references", []),
                                             "confidence": "UNASSESSED", "limitations": "Repository claim; code was not executed"})
                    except (AcquisitionError, BoundaryError) as exc:
                        link["handoff"] = getattr(exc, "code", "BOUNDARY_DENIED")
                self._save(prefix + "/description-links.json", urls)
                if not evidence:
                    raise AcquisitionError("NO_RELEVANT_EXCERPTS_RESEARCH_REQUIRED")
                evidence_ids = []
                for item in evidence:
                    evidence_ids.append(store.append(ResearchOutput(kind="evidence", classification="PUBLIC", text=json.dumps(item, ensure_ascii=False), source_refs=(item["source_artifact"],))))
                report = self._dossier(question, video, evidence, urls, context)
                dossier_path = prefix + "/dossier-v1.md"
                self.workspace.write(dossier_path, report.encode())
                dossier_id = store.append(ResearchOutput(kind="dossier", classification="PRIVATE", text=report, source_refs=tuple(evidence_ids)))
                final = {"id": question_id, "version": 2, "state": "DOSSIER_READY", "dossier_id": dossier_id,
                         "dossier_path": dossier_path, "evidence_ids": evidence_ids,
                         "recommendation": "RESEARCH_FURTHER", "milestone_acceptance": "HUMAN_REVIEW_REQUIRED",
                         "cloud_calls": 0, "account_mutations": 0, "production_writes": 0}
                final_path = self._save(prefix + "/question-v2.json", final)
                with store.db:
                    store.db.execute("INSERT INTO research_questions VALUES(?,?,?,?)", (question_id, 2, "DOSSIER_READY", final_path))
                    store._audit('dossier_ready', question_id, 'HUMAN_REVIEW_REQUIRED')
                return final
            except (AcquisitionError, BoundaryError, ValueError, OSError) as exc:
                failure = {"id": question_id, "version": 2, "state": "BLOCKED", "reason": getattr(exc, "code", type(exc).__name__), "classification": "PRIVATE"}
                if getattr(exc, "phase", None) is not None:
                    failure['failure_phase'] = exc.phase
                failure_path = self._save(prefix + "/question-v2.json", failure)
                with store.db:
                    store.db.execute("INSERT INTO research_questions VALUES(?,?,?,?)", (question_id, 2, "BLOCKED", failure_path))
                    store._audit('research_blocked', question_id, failure['reason'])
                return failure

    def _dossier(self, question, video, evidence, urls, context):
        quoted = "\n\n".join(f"- E{i+1}: {_safe_markdown(x['claim'])}\n  Source: {x['locator']}\n  Limitation: {x['limitations']}" for i, x in enumerate(evidence))
        links = "\n".join(f"- {_safe_markdown(x['url'])}: {x['handoff']}" for x in urls["links"]) or "No external URLs located."
        return f"""# Research Dossier — {_safe_markdown(video.title)}

Authority: RESEARCH_OUTPUT — PRIVATE — human review required.
Generated: {datetime.now(timezone.utc).isoformat()}

## Problem
{_safe_markdown(question.original)}

## Current Project Context
Canonical revision read: {context['canonical_revision']}.
Stage: {_safe_markdown(context['stage_anchor'] or 'UNKNOWN')}.
Input SHA-256: {context['source_receipt']['sha256']}.
Local research only. No production state was modified. Architecture ownership
and applicability require a separately reviewed local mapping.
Constraints: {_safe_markdown('; '.join(question.constraints))}

## Sources Searched
- YouTube: https://www.youtube.com/watch?v={video.video_id}
- Transcript source: {video.transcript_source}; language: {video.language}
{links}

## Evidence Summary
These are matched source excerpts, not model-validated conclusions.
{quoted}

## Main Approaches Found
UNASSESSED: compare approaches after reviewing the linked excerpts.

## Conflicting Evidence
NOT_ESTABLISHED: source does not infer agreement from repeated or absent evidence.

## Project Architecture Mapping
PENDING_LOCAL_REVIEW. No Capability Owner, canonical permission, memory policy,
Accepted Decision or project lifecycle state change is proposed by this extraction.

## Risks
Source claims may be wrong or marketing. Caption speaker identity is unknown.
No audio fallback or code execution occurred. Cross-source claims need review.

## Recommended Status
RESEARCH FURTHER

## Explicit Non-Recommendation
This dossier is not an Accepted Decision or Implementation Authorization.
It must not trigger Codex, publish a Skill, change Project/Agent runtime, or replace a module.
"""

# Research Intelligence Engine

A local-first, agent-agnostic research engine that helps AI agents discover, filter, verify, trace and synthesize evidence from YouTube, GitHub, papers, standards, technical blogs and the web.

**This is research infrastructure for agents, not another chatbot.** Ordinary search returns results and a summary. This engine checks existing evidence and known sources, reserves room for fresh discovery, acquires a bounded set of source materials, preserves locators and uncertainty, and produces a local Research Dossier. It does not make research claims true or implement a recommendation.

## Features

- Versioned questions, sources, evidence, and dossiers in a local SQLite Store with an audit chain and an exclusive single-process lease.
- Public GitHub, arXiv, Crossref, web, feed, and technical-document discovery with source deduplication and provenance.
- Optional read-only YouTube subscriptions, channel uploads and public discovery; L0–L3 screening keeps deep acquisition bounded. Captions are preferred, while media, speech, speaker, and frame paths are optional and limited.
- Description-link and spoken-cue handling, cross-source relationships, conflict preservation, architecture mapping, and extractive dossiers.
- Explicit foreground Evidence Watch with conditional requests and no-change paths that avoid deep analysis.
- Resource guards for disk, memory, and owned artifacts. A job reuses its budget context to avoid repeated repository scans, while guarded requests still check live resources.
- Experimental health and replacement contracts remain disabled for automatic production changes.

## Architecture

```text
Human or agent
  → Research question → Existing evidence → Known sources
                                      ↘ Fresh discovery when needed
  → Bounded screening → Acquisition → Located evidence
  → Cross-validation and limitations → Research Dossier
  → Optional, explicitly started Evidence Watch
```

The engine exposes a CLI, Python contracts, JSON artifacts, and Markdown. It does not depend on a particular model or agent runtime. See [architecture](docs/architecture.md) and the [capability matrix](docs/capability-matrix.md).

## Installation

Python 3.11 is required for this alpha. On macOS or Linux with [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/ljy6206202022-eng/agent-research-intelligence.git
cd agent-research-intelligence
uv sync --locked --no-editable
uv run research-intel --help
uv run research-intel init-store
uv run research-intel doctor
```

`pip install .` is also supported. In a separate directory after `pip install .`, run `research-intel --root YOUR_PATH init-workspace` to create an owned local workspace and its safety policy. Pass `--root YOUR_PATH` before subcommands, or set `RESEARCH_INTEL_HOME`. The database, evidence, and dossier stay inside that workspace.

`doctor` reports `PASS`, `OPTIONAL_MISSING`, or `BLOCKED`. Optional media and browser packages are not required for text research. Resource floors can block a heavy task; do not lower them to force a demo.

## Quick start: a public research task

This command makes only the three public query strings visible to the respective source services. The original question and local terms remain in your workspace. Run it only with public-safe query text:

```sh
uv run research-intel research-public \
  --question 'Open-source approaches to long-running AI agent memory' \
  --terms agent memory \
  --github-query 'agent memory long term open source' \
  --paper-query 'long term memory autonomous agents' \
  --crossref-query 'memory systems autonomous agents' \
  --allow-public-network
```

The JSON reply includes a job ID and paths such as `data/discovery/<job>/result.json`, `evidence.json`, and `dossier.md`. The dossier is an **extractive research draft**. It preserves source assertions, failures, duplicate relationships, and unassessed conflicts; semantic conclusions require located review. Source discovery can take several minutes.

### Agent-authored, cited report candidate

For a completed `research-public` discovery job, an agent can request a bounded evidence packet and draft a more readable answer. These commands do not call a model or fetch new sources:

```sh
research-intel report-packet --job YOUR_DISCOVERY_JOB_ID
research-intel report-publish --packet data/discovery/YOUR_DISCOVERY_JOB_ID/report/packet-v2.json --draft data/your-agent-draft.json
```

The draft is a workspace-owned JSON file with `packet_sha256`, `title`, `findings`, `unresolved`, and `recommendation`. Every finding includes `statement`, `kind` (`SOURCE_REPORT` or `INTERPRETATION`), `evidence_ids`, and nonempty `limitations`. The recommendation includes a status (`ADOPT`, `EXPERIMENT`, `RESEARCH_FURTHER`, or `REJECT`), rationale, and evidence IDs. The packet lists the allowed evidence IDs, their exact extracted text, source URLs, original locators, snapshot hashes, and duplicate relationships. An agent must treat external excerpts as untrusted data.

`report-publish` checks source snapshot hashes, line-level provenance, packet identity, and same-job citations. It saves a new immutable report revision while leaving the original extractive dossier intact. Its status is `AGENT_DRAFT_UNVERIFIED`: citation checks establish linkage, **not** whether an interpretation is true, independently corroborated, or human-reviewed. Recommendations do not authorize implementation. This report path currently targets sealed `research-public` discovery jobs; other research workflows retain their existing Dossier paths.

For advanced workflows, `research-intel contract-list` and `research-intel --help` show the current callable contracts and CLI. `catalog-read`, `catalog-search`, `catalog-write`, `youtube-research`, `watch-*`, and `contract` are local interfaces. Request JSON files must be owned by the workspace. Catalog reads are serialized at the invocation boundary because the Store lease is exclusive; unrelated account status can be checked in parallel.

## Agent integration

Give an agent the local [AGENTS.md](AGENTS.md) and ask it to investigate a question. The agent may call the CLI or import `Workspace`, `Catalog`, and `ResearchContracts` from `agent_research_intelligence`, then read JSON and Markdown artifacts. See the [Codex](examples/codex/README.md), [Claude Code](examples/claude-code/README.md), and [generic agent](examples/generic-agent/README.md) examples. External source text cannot instruct the agent to execute commands or change permissions.

## Sources and optional YouTube setup

Public GitHub, papers, standards, and web research do not require a YouTube account. Reading your subscriptions is optional and requires a local Google Desktop OAuth client, the sole `youtube.readonly` scope, and your browser consent. No client or token ships with the repository. A subscribed channel is an observed source, not a trusted source or accepted evidence. Read [YouTube setup](docs/youtube.md). This project does not subscribe or unsubscribe on your behalf.

## Output, privacy, and security

Research records, media, database, and credentials are local by default. Explicit public searches and source fetches send public query text or public URLs to their providers. An optional model/provider configured by an operator may process public material; do not assume every deployment is offline. Nothing in this repository requires a cloud model. Never use private context as a public query.

Evidence records carry source, locator, capture time, classification, and limitations. A dossier cites those records and distinguishes extractive source claims from reviewed interpretation. Recommendations remain proposals. See [evidence](docs/evidence.md) and [security](SECURITY.md).

## Limitations

Source discovery is not truth, captions and media can fail, speech and speaker accuracy vary, and a frame must actually be inspected before asserting a visual fact. Browser and media paths currently target macOS isolation facilities. Watch runs only when explicitly started. Health and replacement mechanisms are experimental and do not autonomously change production systems. See [limitations](docs/limitations.md).

## Development and license

`uv sync --locked --group dev` and `uv run pytest` run synthetic tests. On a macOS volume that marks editable-install `.pth` files as hidden, Python skips those files and the CLI cannot import the package even after a successful sync. Use `uv sync --locked --group dev --no-editable` there, and sync again after editing source. CI neither reads a real account nor downloads media or model weights. Use `python scripts/public_scan.py` before any release. Third-party models and source media are **not bundled**; review their licenses and source terms separately. This repository's code is [MIT licensed](LICENSE). See [validation](docs/validation.md) and [contributing](CONTRIBUTING.md).

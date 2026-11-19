# Architecture

## Context

EventLens is a separate product from Vibe-Trading. It treats Vibe-Trading as an
external research engine whose output is useful but non-canonical until
attributed and validated. EventLens owns the analyst workflow, financial
calculations, session state, artifacts, and audit trail.

## Dependency direction

```text
Streamlit / CLI
      |
Product-session service
      |
Repository transitions -- Agent export/import
      |
Domain analytics
      |
Provider protocols ---- Optional Vibe evidence protocol
      |                         |
fixtures / live data     allowlisted MCP transport
      |
Artifact, repository, and audit stores
```

Outer layers may depend on inner protocols and domain types. Domain analytics
must not import Streamlit, a model SDK, a market-data vendor, or
Vibe-Trading.

## Proposed package boundaries

```text
src/eventlens/
  app/                 use cases and CLI commands
  graph/               read-only legacy state models for artifact compatibility
  domain/              event logic and deterministic calculations
  agents/              prompts, structured interpretation, claim validation
  research_engines/    optional allowlisted Vibe evidence adapter and schemas
  providers/           source protocols, fixtures, live/local adapters
  artifacts/           schemas, lineage, rendering, atomic store
  persistence/         durable records and append-only audit
  evaluation/          replay and agent-quality evaluation
  config/              validated settings
```

## Canonical data rules

1. Canonical numeric metrics originate only from versioned deterministic
   functions.
2. Raw provider and Vibe-Trading payloads are stored by reference and checksum,
   not duplicated throughout product-session records.
3. Every normalized datum carries origin, retrieval time, coverage, request
   parameters, warnings, and raw reference/checksum.
4. `null` plus a reason represents missing data; invented substitutes are
   invalid.
5. Switching to a proxy or degraded mode is an explicit, audited decision.
6. The original locked hypothesis is immutable.
7. Post-lock annotations and archived-session annotations are separate versions.

## External-agent routing and packet ownership

The accepted R1 contract is recorded in
[`decisions/0005-external-agent-routing-and-packet-contracts.md`](decisions/0005-external-agent-routing-and-packet-contracts.md).
In summary:

- `EVENTLENS_EXTERNAL_AGENT_ENABLED=true` selects an export, durable pause, and
  explicit import route. EventLens must not invoke an external agent.
- `EVENTLENS_EXTERNAL_AGENT_ENABLED=false` selects DeepSeek explicitly. It uses
  the same bundle, report schema, and import validator; it is not a fallback.
- EventLens owns canonical facts, calculations, state transitions, validation,
  artifact checksums, and the final `DecisionPacket`.
- An `ExternalAgentReport` is a semantic proposal. It cannot become a
  `DecisionPacket`, mutate the exported facts, or advance workflow state until
  deterministic import validation succeeds.
- Product-session records retain immutable artifact IDs/checksums and status,
  not raw article bodies or an unrestricted agent prompt/completion.

## Workflow invariants

- Post-event data is inaccessible to pre-event nodes until hypothesis lock.
- All required approvals are durable repository transitions, not transient UI state.
- Commands that call external systems or write artifacts are idempotent.
- State contains artifact references rather than transcript/options payloads.
- Archive is append-only and terminal for the canonical session record.
- Vibe-Trading warnings and failures remain visible downstream.

## Storage plan

Use a filesystem store for the MVP:

```text
artifacts/<session-id>/
  session_manifest.json
  event_packet.json
  hypothesis_snapshot.json
  pre_earnings_brief.json
  pre_earnings_brief.md
  volatility_setup.json
  transcript_delta.json
  post_earnings_review.json
  post_earnings_review.md
  validation_report.json
  validation_report.md
  audit_log.jsonl
  raw/
```

Writes use a temporary file, validation, `fsync` where appropriate, and atomic
rename. Product state uses immutable `ResearchMemoryRepository` revisions;
integration tests reopen sessions in a fresh service/process to verify restart
behavior.

## Security and privacy baseline

- Bind local services to loopback by default.
- Never commit credentials or raw provider tokens.
- Redact secrets from commands, logs, errors, and manifests.
- Treat transcript and research text as untrusted input to model prompts.
- Disable arbitrary generated-code execution in the product path.
- Allowlist Vibe-Trading executable, transport, data sources, and output roots.
- Record model/provider identifiers but define an explicit policy before storing
  full prompts or completions.

## Technology decisions still requiring spikes

- Trading-calendar library and earnings timing edge cases.
- Dataframe library for canonical fixture serialization.
- Whether a future Vibe REST/SSE transport offers better attributed research
  lineage than the restricted MCP core call.
- Production model choice between the implemented DeepSeek and Moonshot/Kimi
  adapters; fixture replay remains independent of this choice.

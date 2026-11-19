# Decision 0002: Defer the Deep Agent follow-up investigator

## Status

Accepted — post-MVP design, not implemented.

## Decision

EventLens will keep the MVP follow-up path bounded to the retained evidence
bundle and the existing deterministic read-only tools. A future Deep Agent may
be introduced as a specialized follow-up evidence investigator, but it will be
implemented only after the report-first workflow, live-provider path, and local
UI are stable.

The future investigator is a bounded subagent, not a replacement for the
canonical EventLens workflow. It may investigate an analyst's follow-up
question, retrieve additional approved evidence, and return a cited evidence
packet. It may not change the locked hypothesis, canonical metrics, event
dates, request IDs, idempotency keys, or approved report.

## Intended future flow

```text
approved report + follow-up question
  -> Deep Agent investigator
  -> allowlisted read-only tools/providers
  -> evidence packet with source IDs, timestamps, and citations
  -> EventLens claim/evidence validator
  -> answer or request for analyst approval when scope expands
```

The investigator should run with a separate context window and an explicit
scope derived from the approved session. It should not receive post-event data
when the session is still pre-event, and it should not be able to execute
trades, write arbitrary files, mutate workflow state, or call unapproved
providers.

## Why defer it

- The MVP already has a durable LangGraph workflow, retained evidence, direct
  DeepSeek/Kimi adapters, citation validation, and follow-up continuity.
- A general-purpose subagent would add tool-selection, prompt-injection,
  quota, latency, and evaluation risk before the core live path is proven.
- The current explicit graph is easier to replay and audit for financial use.
- The additional investigator is most valuable once the UI can show its scope,
  sources, progress, and approval boundary clearly.

## Design constraints for later implementation

1. Use a dedicated `FollowUpInvestigationRequest` and
   `FollowUpEvidencePacket` schema.
2. Preserve the original `session_id`, report version, information cutoff, and
   locked hypothesis as immutable context.
3. Allow only read-only, source-attributed tools with provider and date-range
   limits.
4. Require every returned factual or interpretive statement to cite a supplied
   evidence passage.
5. Keep canonical arithmetic and report mutation in EventLens-owned code.
6. Cache investigator requests and results by a complete request digest.
7. Add evaluation cases for unsupported expansion, hindsight leakage, stale
   evidence, prompt injection in retrieved text, and quota exhaustion.

## Revisit trigger

Revisit this decision after the MVP has a working live-provider run and local
UI, and when users need follow-up questions that cannot be answered from the
retained evidence bundle alone.

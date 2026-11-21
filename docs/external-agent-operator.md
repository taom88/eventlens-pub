# External-agent operator contract

This procedure lets any fresh agent analyze EventLens evidence without prior
repository or conversation context.

> **Version 1 compatibility note:** This procedure and its schema describe the
> current legacy external-report contract. That schema has a fixed action
> vocabulary for compatibility; it is not a default trading policy. A
> publication-quality policy-governed recommendation workflow is not complete
> in this preview. Treat any report as research for human review, and abstain
> when no applicable operator policy supports a stance.

## Inputs

Provide the agent only:

1. the exported `ExternalAgentBundle` JSON;
2. `schemas/eventlens.external_agent.v1.schema.json`;
3. the stable prompt below.

Do not provide API keys, `.env` files, post-cutoff evidence, target-event
outcomes, unrestricted source bodies, or mutable local file paths.

## Stable prompt

> Read the supplied EventLens bundle as an immutable evidence boundary. Return
> exactly one JSON object matching `ExternalAgentReport` in the supplied schema.
> Use only exported evidence and canonical metric IDs. Cite every material claim
> with an exported `source_id` and `passage_id`. Keep facts, interpretation,
> uncertainty, contradicting evidence, and missing evidence distinct. The v1
> schema supports legacy research-only action labels; do not treat them as an
> EventLens-owned policy or use them to generate an action without an applicable
> operator policy. Otherwise abstain. Precise valuation zones require
> calculation or metric references. Do not alter bundle facts, infer
> post-cutoff outcomes, provide execution instructions, add schema fields, or
> return Markdown.

## Run sequence

Start from the universal research-evidence bundle. This command validates the
bundle, creates the external-agent handoff, and writes the session transition
through the configured repository before returning
`awaiting_external_agent`:

```bash
eventlens --env-file .env research-ask \
  --question "Prepare research for the next company event" \
  --ticker SYMBOL \
  --evidence-bundle research-evidence.json \
  --output-dir .eventlens-cache/research-asks \
  --session-id SESSION_ID
```

With the default feature flag, download the emitted bundle from the session and
give it to the agent. Save the returned JSON, then use the same product-session
root for the explicit import:

```bash
eventlens --env-file .env research-import-agent-report \
  --session-id SESSION_ID \
  --report external-agent-report.json \
  --output-dir .eventlens-cache/research-asks
```

When `EVENTLENS_EXTERNAL_AGENT_ENABLED=false`, `research-ask` explicitly uses
DeepSeek through the identical bundle, report schema, validator, and audit path.
Validation failure never triggers cross-mode fallback.

For deployed instances, set `EVENTLENS_MEMORY_BACKEND=supabase`. Supabase owns
the immutable typed session/run graph; the server-side artifact directory owns
checksum-addressed bundle and report bodies in Phase 3. A second machine can
read the graph but cannot reconstruct those bodies unless the artifact
directory is also mounted or copied. Shared object storage is a later boundary,
not an implicit public Supabase bucket.

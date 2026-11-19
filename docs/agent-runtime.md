# Agent runtime and operations

## Runtime boundary

`ProductSessionFacade` is the sole product command and transition owner. CLI
and Streamlit submit or reopen the same repository-backed session; neither
adapter calculates domain facts or writes status independently.

The canonical pre-event flow is:

1. Resolve a plain-language research request against a supplied
   `ResearchEvidenceBundle` or the fixture input adapter.
2. Enforce cutoff-safe prior context, ontology identity, evidence rights, and
   deterministic metrics before export.
3. Persist an immutable product-session revision and artifact references.
4. In external-agent mode, export the typed bundle, persist
   `awaiting_external_agent`, and stop.
5. On explicit report import, validate bundle identity, metrics, citations,
   cutoff, policy fields, and schema before committing the next revision.
6. Reconcile an immutable handoff that committed before a repository crash on
   the next load, without repeating provider or model calls.

Repository revisions are authoritative. Local session JSON is a verified
mirror or one-time legacy import source. The external handoff store owns bundle
and report bytes; it does not own product status.

## Agent routing

`EVENTLENS_EXTERNAL_AGENT_ENABLED=true` is the default. EventLens exports a
bundle and waits for an explicit import; it never launches an external agent.
When the flag is false, the configured DeepSeek adapter uses the same typed
contract and validation gate. There is no silent fallback between routes.

The optional Vibe integration is `VibeMcpEvidenceClient`. It exposes only the
reviewed read-only evidence tools, validates discovered schemas, applies the
information cutoff, and records checksum-addressed receipts. It is an evidence
adapter, not a lifecycle or packet writer.

## Operator commands

The key-free demonstration is:

```bash
eventlens demo
```

A live/cached research session begins from a validated evidence bundle:

```bash
eventlens research-ask \
  --question "Prepare company-event research" \
  --ticker SYMBOL \
  --evidence-bundle /path/to/research-evidence.json \
  --output-dir .eventlens-cache/research-asks
```

External-agent output is imported explicitly:

```bash
eventlens research-import-agent-report \
  --session-id SESSION_ID \
  --report /path/to/report.json \
  --output-dir .eventlens-cache/research-asks
```

Use `config-check`, `provider-plan`, `agent-check`, and `vibe-check` for
secret-safe readiness checks. Credentialed provider calls are never part of
ordinary CI.

## Retained compatibility boundary

`src/eventlens/graph/models.py` remains readable because stored legacy records
and learning schemas reference its Pydantic models. No executable graph,
checkpoint writer, graph CLI, graph UI, or state-coupled packet assembler is
retained. New lifecycle behavior must extend the product-session service and
repository transition contract.

# EventLens analyst web interface

> Vibe-Trading is the laboratory. EventLens is the earnings-specific research
> protocol and decision ledger.

## Start locally

```bash
source .venv/bin/activate
EVENTLENS_ENV_FILE=.env \
streamlit run src/eventlens/web/streamlit_app.py --server.port 8501
```

The console uses the same settings as the CLI and never renders credential
values. Run `eventlens config-check` and `eventlens provider-plan` before using
Live mode.

## Modes

- **Fixture** opens the checksum-bound D0 journey with three readable context
  cards, a deterministic baseline, and an external-agent pause. It makes no
  provider or model calls.
- **Live** uses the canonical product-session service and configured evidence
  source. Missing readiness is displayed as a typed blocked state; it is not
  replaced with fixture data.

The retired Mocked-MCP/graph route is not a selectable product mode. Optional
Vibe evidence continues through the allowlisted evidence adapter.

## Screen map

1. **New session** submits a plain-language assignment to
   `ProductSessionFacade`.
2. **Overview** shows the durable stage, origin, readiness, event, cutoff, and
   persistence backend.
3. **Prior context** renders executive summaries, key messages, plain-language
   metric/reaction interpretation, caveats, provenance, and downloads.
4. **Deterministic baseline** separates signed reaction, absolute movement,
   benchmark/cost supplements, mechanical guardrails, and valuation
   missingness.
5. **External handoff** downloads the verified bundle and explicitly imports a
   typed report. External mode never invokes the agent automatically.
6. **Artifacts** verifies checksums and renders a secret-safe audit trace.
7. **Unavailable stages** names synthesis, QA, packet, and post-event work that
   has not yet been implemented.

## Ownership and restart

```text
Streamlit rendering and analyst actions
        │
        ▼
ProductSessionFacade
        │
        ├── ResearchEvidenceBundle / providers
        ├── ExternalAgentHandoffStore
        └── ResearchMemoryRepository revisions
```

The UI performs no financial calculation and does not write product status.
Browser refresh or process restart is handled by entering the same session ID
and choosing **Resume durable session**. Repository revisions are authoritative;
artifact files and the local session document are checksum-verified references
or mirrors.

## Testing

```bash
.venv/bin/python -m pytest -q tests/web
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check
```

Coverage includes the key-free product journey, explicit report import,
fresh-app resume, invalid import handling, background-job responsiveness,
secret-safe rendering, context/baseline readability, and artifact integrity.

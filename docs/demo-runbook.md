# D0 fixture replay runbook

The key-free D0 journey uses the checked-in DEMO family at
`fixtures/d0/demo/`. It is synthetic demonstration data and must not be
described as live provider or model output.

## Five-minute Streamlit journey

1. From the repository root, run `./.venv/bin/streamlit run src/eventlens/web/streamlit_app.py`.
2. Leave **Research mode** set to **Fixture**. The page must identify the run
   as a key-free D0 replay and must not claim live provenance.
3. Keep the Example Systems request and select **Start product research**. The browser
   remains responsive while the background job runs; select **Refresh
   progress** until the durable session opens.
4. Verify that **Prior context** contains exactly three readable historical
   cards, each with an executive summary, messages, reaction interpretation,
   provenance, rights warning, and JSON/Markdown downloads.
5. Verify that **Deterministic baseline** labels signed reaction separately
   from absolute move, reports the three-event sample and unavailable CI, and
   labels mechanical guardrails as non-valuation.
6. In **External handoff**, download the checksum-verified bundle. The state
   must be `awaiting_external_agent`; no outside model is invoked.
7. Upload `fixtures/d0/demo/report.json`, select **Validate and import
   report**, and verify that the state becomes `validated` with JSON and
   Markdown report downloads.
8. Copy session ID `d0-fixture-demo-2026q3`, restart Streamlit, enter that ID
   under **Resume session ID**, and select **Resume durable session**. The same
   validated state, checksums, cards, and baseline must reopen.
9. Open **Unavailable stages**. Company ontology, content-aware news,
   valuation, recommendation synthesis, committee/QA, and post-event scoring
   must remain explicitly unavailable rather than being represented by generic
   prose.

This journey is deliberately research-only. It never places or simulates an
order and it consumes zero provider or model calls.

## Headless acceptance helper

Run the replay from the repository root:

```bash
.venv/bin/python -c "from pathlib import Path; from eventlens.product.fixture_replay import run_d0_demo_fixture_replay; r=run_d0_demo_fixture_replay(Path('/tmp/eventlens-d0-handoff')); print(r.imported.status, r.reopened.session_id)"
```

The end-to-end interface is `run_d0_demo_fixture_replay(store_root,
fixture_root=None)`. For a genuine UI pause before an explicit report import,
use `prepare_d0_demo_fixture_replay(store_root, fixture_root=None)`; it returns
the assembled package and an `awaiting_external_agent` state without importing
the report. The completion helper then imports the report and reopens state.
Both paths load exactly three readable prior context cards, verify the fixed signed
and absolute baseline plus its explicitly non-valuation guardrails, assembles
the external bundle through `assemble_external_bundle`, exports and pauses via
`ExternalAgentHandoffStore`, imports the checked-in fixture report through the
production validator, and reloads state from a new store instance.

The manifest records `origin: fixture_replay`, `provider_calls: 0`, and
`model_calls: 0`. Future semantic stages remain in `unavailable_stages`; the
fixture runner never substitutes generic prose for them.

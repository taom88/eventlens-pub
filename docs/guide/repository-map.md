# Repository map

This guide groups files by the question they answer. Paths below are relative
to the repository root.

## Start with a behavior, not the whole tree

For a focused code change, trace one user-visible behavior through four places:

1. **Contract:** a Pydantic model or JSON schema that states valid data.
2. **Owner:** the service or validator that applies the contract.
3. **Adapter:** CLI, Streamlit, provider, or persistence code that connects it
   to the outside world.
4. **Proof:** a test showing the expected success, failure, or boundary.

This keeps interface code thin and helps distinguish an accepted input from a
canonical fact or a rendered summary.

## Product code

| Path | Responsibility | Good first file or search |
|---|---|---|
| `src/eventlens/app/` | CLI command parsing and dispatch | `commands.py`; search for a command name |
| `src/eventlens/product/` | Product workflows, evidence contracts, collection assembly, and research request behavior | `research_evidence.py`, `live_evidence_collection.py`, `ask.py` |
| `src/eventlens/web/` | Streamlit presentation, jobs, and the shared product-session service | `product_session.py`, `streamlit_app.py` |
| `src/eventlens/domain/` | Domain values and reusable event/business rules | Search for the event or metric type under change |
| `src/eventlens/quant/` | Deterministic event, market-reaction, volatility, and related calculations | `event_study.py`, `backtest.py`, and the model/input modules |
| `src/eventlens/context_cards/` | Historical context card models, builders, quality checks, and rendering | `models.py`, `history.py`, `rendering.py` |
| `src/eventlens/providers/` | Provider protocols, registries, free/local adapters, and live integrations | `registry.py`, `base.py`, then the relevant adapter |
| `src/eventlens/agents/` | Agent routes, typed report contracts, citation handling, and import validation | `external.py`, `handoff.py`, and report validators |
| `src/eventlens/research_engines/` | Optional Vibe-Trading evidence integration behind an EventLens-owned protocol | `vibe_evidence.py` |
| `src/eventlens/artifacts/` | Artifact models, checksum checks, replay output, and storage support | `models.py`, `store.py`, `replay.py` |
| `src/eventlens/memory/` and `src/eventlens/persistence/` | Repository interfaces/adapters and typed durable record contracts | `sqlite.py`, `run_records.py`, migrations under `supabase/` |
| `src/eventlens/learning/` | Readiness, evaluation records, graders, scorecards, and shadow policy logic | `readiness.py`, `dataset.py`, `scorecards.py` |
| `src/eventlens/graph/` | Legacy graph-shaped Pydantic models kept for stored-record/schema compatibility | `models.py`; it is not the active lifecycle runtime |

The module map is an orientation aid, not a promise that every path is part of
the supported public API. Confirm imports and tests before extending a boundary.

## Product and interface contracts

| Path | What it defines |
|---|---|
| `schemas/` | Versioned JSON schemas for context cards, evidence, and agent exchange |
| `fixtures/d0/demo/` | Synthetic end-to-end fixture used by the public `eventlens demo` path |
| `fixtures/evals/` | Controlled evaluator and quality-check examples; see fixture metadata before making quality claims |
| `docs/guide/` | First-time orientation, product model, decision rationale, status, and repository map |
| `docs/decisions/` | Selected architecture and contract records; some records are explicitly legacy or partially superseded |
| `docs/evaluations/` | Evaluation rubrics and example score/evidence records |
| `docs/public-release/` | Preview scope, live setup, dependency-license evidence, and snapshot acceptance record |
| `docs/` | Operator, architecture, storage, provider, evaluation, and interface documentation |

## Tests

The `tests/` tree mostly mirrors these boundaries:

- `tests/unit/` focuses on small domain rules.
- `tests/context_cards/`, `tests/quant/`, and `tests/providers/` cover their
  respective model and deterministic/adapter contracts.
- `tests/product/`, `tests/persistence/`, `tests/memory/`, and `tests/web/`
  exercise workflow, storage, and presentation boundaries.
- `tests/integration/` checks assembled flows such as fixture replay and
  external-agent exchange.
- `tests/evals/` checks evaluator schemas and synthetic benchmark behavior.

CI uses deterministic tests and non-secret checks. It does not call live
providers as part of ordinary pull-request verification.

## Files that affect installation and use

- `pyproject.toml` declares runtime and optional dependencies, the `eventlens`
  command, wheel contents, and test defaults.
- `.env.example` documents settings without supplying credentials. Copy it to
  ignored `.env` for local configuration and never commit real values.
- `.github/workflows/ci.yml` runs the Linux Python matrix, tests, compile check,
  public-release scan, and wheel build.
- `THIRD_PARTY_NOTICES.md` describes the separate dependency boundary and links
  to the inventory and review.
- `LICENSE`, `NOTICE`, `DATA_POLICY.md`, `PRIVACY.md`, `SECURITY.md`, and
  `DISCLAIMER.md` govern the public source preview and its use.

## Trace the public demo

To see the project as a newcomer can run it:

```text
eventlens demo
  -> src/eventlens/app/commands.py
  -> src/eventlens/product/fixture_replay.py
  -> fixtures/d0/demo/
  -> ProductSessionFacade and ExternalAgentHandoffStore
  -> typed synthetic report validation and session reopen
  -> JSON smoke summary
```

The source of detail is the implementation and tests, not the printed summary
alone. The [demo runbook](../demo-runbook.md) names the screens and checks to
inspect.

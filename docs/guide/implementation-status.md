# Implementation status and preview boundaries

EventLens has several working contracts and demonstrations, but they do not add
up to a finished end-to-end financial research service. This page separates
what can be tried today from work that remains outside this public preview.

## Available paths

| Area | What is implemented | How to verify or learn more |
|---|---|---|
| Key-free package smoke | `eventlens demo` replays a checked-in synthetic journey and reports its call counts | Run the [quick start](../../README.md#quick-start); inspect `src/eventlens/product/fixture_replay.py` and `tests/integration/test_d0_fixture_replay.py` |
| Local analyst interface | Fixture and live modes use the product-session service; sessions can be resumed and artifacts inspected | [Web interface](../web-interface.md) and [demo runbook](../demo-runbook.md) |
| Session and artifact integrity | Typed status revisions, idempotent creation, artifact references, checksum verification, and validation boundaries | `src/eventlens/web/product_session.py`, `src/eventlens/artifacts/`, and corresponding tests |
| Historical context and event analytics | Typed cards, cutoff/rights checks, deterministic reaction calculations, and synthetic evaluation cases | `src/eventlens/context_cards/`, `src/eventlens/quant/`, and `docs/evaluations/` |
| Live evidence collection | Provider adapters and explicit manifest/run-config workflow, including timing, budget, rights, and authorization checks | [Live-provider setup](../public-release/live-provider-setup.md) |
| Research bundle and agent handoff | Validated `ResearchEvidenceBundle`, default external pause, explicit report import, and configured direct DeepSeek route | [External-agent contract](../external-agent-operator.md) and `src/eventlens/agents/` |
| Persistence | Local SQLite for demo/test and owner-configured Supabase for durable typed records | [Storage](../storage.md) and [Supabase setup](../supabase-setup.md) |
| Learning/evaluation structures | Offline validators, scorecards, synthetic fixtures, and shadow/candidate governance contracts | [Learning and evaluation](../learning-and-evaluation.md) |

This table describes components and tested paths. It does not imply that all
components are composed into one production-hardened live workflow.

## What is not established by this repository

- A complete live, multi-provider research journey from an ordinary user
  question through final human-reviewed and publishable investment research.
- Reliable data access for a particular operator, issuer, provider account, or
  deployment. Provider limits and terms can change and depend on the account.
- General research quality, forecast accuracy, investment returns, or a causal
  benefit from context cards. The synthetic context-card evaluator has no
  human-scored benchmark in this release.
- Automatic committee approval, production QA/release governance, or a
  self-updating policy. The learning design remains offline/shadow and requires
  human promotion.
- Shared hosted storage or a managed service. Each operator must supply
  deployment infrastructure and secure artifact storage.
- Legal clearance for provider data, bundled distributions, or a business
  model. License inventory work is an engineering review for the current
  separately installed dependency boundary.

The public acceptance record notes one limited Massive news connection smoke.
That does not validate the full live collection path, every Massive capability,
other providers, or a complete research run.

## Evaluation and learning

The repository includes multiple kinds of checks that answer different
questions:

1. **Unit and integration tests** check code and contracts on controlled or
   synthetic inputs.
2. **Context-card fixtures** exercise known examples against a rubric. The
   current public cases are synthetic and are not a human-scored accuracy set.
3. **Readiness audit** reports whether retained outcomes and postmortems are
   sufficient for offline learning. Run `eventlens learning-audit` against the
   configured artifact directory for a current local result.
4. **Offline/shadow comparison** is the intended route for comparing a
   candidate with a fixed baseline; it does not apply the candidate to live
   decisions.

The figures under “Observed readiness” in
[`learning-and-evaluation.md`](../learning-and-evaluation.md) are dated
observations from a local artifact set, not a live repository metric. Check the
date and run the audit before repeating those counts. A passing test suite or
small synthetic evaluator run does not demonstrate product-level quality.

## Running live collection is a separate authorization step

Live use requires the operator to prepare and verify an event contract, obtain
any provider credentials, review data rights and budgets, and explicitly
authorize the configured collection. The scripts fail closed on invalid
checksums, inconsistent identity, timing violations, or missing authorization.
Follow the full [live-provider setup guide](../public-release/live-provider-setup.md);
do not turn the synthetic fixture files into live inputs by editing only the
ticker.

## Release boundaries

This repository is an alpha source preview. Its README, tests, scanner, and
license inventory are intended to help a reviewer understand the code and
reproduce its synthetic smoke path. They do not establish a complete product
milestone, provider data rights, or formal legal clearance. See the
[acceptance record](../public-release/acceptance-note.md) for the precise
snapshot verification record and its limits.

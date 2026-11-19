# Product decisions

EventLens favors traceable inputs, explicit limits, and reversible human
decisions over an opaque “give me the answer” flow. These are the decisions a
new reader is most likely to need when evaluating the design or proposing a
change.

## 1. Event research is time-bounded

**Decision:** Every pre-event analysis has an information cutoff. Evidence and
prior context must be eligible before it.

**Why:** Earnings and other scheduled events create a clear risk of hindsight
contamination. A historical replay should resemble the information available
to an analyst at the time, not the information that later became convenient.

**Engineering consequence:** Inputs carry publication/availability timestamps,
event and cutoff identity, and provenance. Validators reject post-cutoff
receipts and ineligible cards. Outcome data belongs to a later retrospective
record, not the pre-event evidence bundle.

**Where to inspect:** `src/eventlens/product/research_evidence.py`,
`src/eventlens/product/live_evidence.py`, and the cutoff tests under
`tests/product/`, `tests/context_cards/`, and `tests/quant/`.

## 2. EventLens owns arithmetic and state transitions

**Decision:** Deterministic code owns canonical metrics, units, event timing,
checksums, schema validation, and workflow status. An agent contributes
bounded semantic interpretation.

**Why:** A model can help explain a statement or compare a thesis with evidence,
but it should not silently rewrite a calculation, change the event being
analyzed, or advance a persisted workflow after an invalid response.

**Engineering consequence:** The report schema refers to canonical facts and
allowed citations. Import validates those references before committing a new
session revision. Presentation code renders product state; it does not become
the owner of financial calculations or workflow transitions.

**Where to inspect:** `src/eventlens/web/product_session.py`,
`src/eventlens/product/synthesis_input.py`, `src/eventlens/agents/`, and
`src/eventlens/committee/`.

## 3. Missingness is a result, not a hole to fill

**Decision:** Missing, partial, conflicted, or incomparable inputs remain
visible with a typed status or reason. A proxy is never silently represented as
the original data.

**Why:** A polished number with an undisclosed basis can be more misleading
than an explicit gap. Evidence availability varies by issuer, provider, period,
and entitlement.

**Engineering consequence:** Validators require corresponding missingness in
many incomplete cases. Proxy, degraded, or incomparable modes are labeled in
the output, and unsupported calculations fail or remain unavailable.

**Where to inspect:** the models in `src/eventlens/product/research_evidence.py`,
`src/eventlens/quant/`, and the provider response/coverage types in
`src/eventlens/providers/`.

## 4. Prior context is a gate, not decoration

**Decision:** A live pre-event request normally needs three approved,
cutoff-eligible prior cards. A newly covered issuer may take an explicit
insufficient-history route, but the sample limit must remain visible.

**Why:** Context is intended to arrive before analysis so it can inform the
analyst’s reasoning. Attaching it later as a retrospective footnote defeats
that purpose. Sample size also matters: a handful of events cannot support
strong claims about stable issuer behavior.

**Engineering consequence:** Bundles bind prior-card IDs, status, cutoffs, and
checksums. Missing history cannot pass without the new-coverage eligibility
record and matching missingness marker.

**Where to inspect:** `src/eventlens/product/research_evidence.py`,
`src/eventlens/memory/sqlite.py`, and `tests/product/` and
`tests/memory/`.

## 5. Rights and retention travel with evidence

**Decision:** A source reference, checksum, or stored copy is not permission to
use or redistribute that content. Evidence must carry the relevant source,
availability, and rights/retention information; raw source bodies are bounded
or omitted when the data contract requires it.

**Why:** Market, news, filing, transcript, and provider content may be subject
to different terms. A technically accessible response is not automatically
available for permanent storage or publication.

**Engineering consequence:** Collection adapters, receipt types, and assembly
validators preserve rights status and provenance. The public source release
uses synthetic fixtures and does not include live provider receipts. Operators
must review each provider’s current terms and maintain their own credentials.

**Where to inspect:** [Data policy](../../DATA_POLICY.md),
[provider setup](../public-release/live-provider-setup.md),
`src/eventlens/providers/`, and `src/eventlens/product/research_evidence.py`.

## 6. Agent use is explicit and validated

**Decision:** The default agent path is a human-operated handoff: export one
immutable bundle, persist a waiting state, and stop. Direct DeepSeek is a
separately selected route that shares the same report validator. There is no
silent fallback between agents or modes.

**Why:** An analyst should know which system received the evidence, see the
exact bundle, and decide when to import its result. A network or validation
failure should not quietly change the selected model or route.

**Engineering consequence:** External report files are proposals. They cannot
change bundle facts, cutoff, session identity, or workflow state until they
pass deterministic validation. EventLens never launches the external agent on
the operator’s behalf.

**Where to inspect:** [External-agent operator contract](../external-agent-operator.md),
`src/eventlens/agents/`, and `src/eventlens/web/product_session.py`.

## 7. The preview demonstrates contracts, not production readiness

**Decision:** The public demo is synthetic and offline. Live collection is a
separate, explicitly configured operation; a test or demo success is not
presented as evidence of live data quality, model quality, or investment
performance.

**Why:** Users need a reliable way to explore the code without credentials or
provider calls. At the same time, a working fixture must not imply the entire
live review and publishing lifecycle is complete.

**Engineering consequence:** Fixture and live origins are distinct; the demo
reports zero calls. Live collection requires an owner-reviewed manifest,
cutoff, request budget, provider rights, and explicit authorization. CI uses
mocked/synthetic boundaries rather than credentialed provider calls.

**Where to inspect:** [Demo runbook](../demo-runbook.md),
[live-provider setup](../public-release/live-provider-setup.md),
[preview limits](../public-release/README.md), and
[implementation status](implementation-status.md).

## 8. Learning remains offline and shadowed

**Decision:** Evaluation code may compare candidates against fixed baselines,
but a candidate cannot silently change the live workflow. Promotion is a
separate human-governed step and requires adequate, independent outcome data.

**Why:** Small, correlated event samples can produce unstable apparent wins.
Metrics can improve because data coverage, market regimes, provider choices,
or prompts changed, not because the system’s decision quality improved.

**Engineering consequence:** The learning design records versions, samples,
outcomes, uncertainty, and safety results; it uses offline/shadow evaluation.
Synthetic fixtures test the evaluator’s contracts but do not establish a
performance improvement.

**Where to inspect:** [Learning and evaluation](../learning-and-evaluation.md),
`src/eventlens/learning/`, and
`docs/evaluations/context-card-improvement-loop-v0.1.md`.

## Decision records and how to read them

The files in [`docs/decisions/`](../decisions/) explain selected architecture
and contract decisions. They are not a complete current product specification.
In particular, `0005-external-agent-routing-and-packet-contracts.md` records a
versioned legacy report contract and explicitly identifies later superseding
decisions for some fields. Use current schemas and validators for the
implemented behavior; read decision records as rationale and history, not as
permission to revive superseded defaults.

When a guide and code disagree, check the current model, validator, and test
before changing behavior. When a proposed change crosses a product boundary,
update the relevant schema, tests, and explanation together.

# ADR 0005: freeze external-agent routing and packet contracts

- Status: accepted for R1 implementation
- Date: 2026-10-24
- Tickets: MVP-002, governing MVP-101 through MVP-107

ADR 0009 supersedes this ADR's transition-owner paragraph. ADR 0010 supersedes
its built-in action-vocabulary requirements for new synthesis contracts. The
version 1 bundle/report contract below remains immutable and readable as a
legacy compatibility contract; its fixed labels are not defaults for new runs.

## Context

EventLens needs one portable research-synthesis boundary that works with a
user-operated external agent and with the in-process DeepSeek adapter. The
external-agent path is deliberately asynchronous: EventLens exports immutable,
cutoff-safe inputs and stops. It never starts an external agent.

The repository already contains useful but narrower contracts:

- `AnalystInterpretationRequest` and `AgentReportDraft` support evidence-linked
  report prose but deliberately reject recommendation language.
- `ArtifactEnvelope` owns common storage lineage and payload checksums.
- `WorkflowState` owns durable transitions and currently embeds the existing
  committee `DecisionPacket`.
- `DecisionPacket` owns committee synthesis and dissent, but does not yet carry
  the refined MVP recommendation, valuation, or QA release fields.

These types must not be treated as interchangeable with the new R1 bundle and
report.

## Decision

### Route selection

Route selection is a pure configuration decision made before bundle handling:

| `EVENTLENS_EXTERNAL_AGENT_ENABLED` | Selected route | Required behavior |
|---|---|---|
| `true` (default) | `external` | Build and export one immutable bundle, persist `awaiting_external_agent`, and stop. Resume only after an explicit import for that session and bundle. |
| `false` | `deepseek` | Submit the identical bundle payload to the configured DeepSeek adapter, normalize the response to `ExternalAgentReport`, and call the identical import validator. |

The route is fixed for a bundle revision. A missing, late, malformed, or rejected
external report never causes a DeepSeek call. A DeepSeek failure never changes
the route to external or another provider. Legacy `agent_provider` values may
remain for existing workflows, but they do not override this R1 routing table.
Flag-off R1 readiness requires DeepSeek configuration specifically.

### Ownership

`ExternalAgentBundle` is an EventLens-owned, immutable input snapshot. It may
contain bounded, rights-permitted passage text needed to make the handoff
self-contained; unrestricted source bodies, secrets, mutable paths, target-event
outcomes, and post-cutoff evidence are prohibited.

`ExternalAgentReport` version 1 is agent-authored semantic output. It may
propose claims, scenarios, forecasts, valuation zones, and its legacy
research-only action, but cannot
alter bundle facts, calculations, event identity, cutoff, evidence, or workflow
state.

`DecisionPacket` in `eventlens.committee.packet` remains the sole canonical final
packet. Later R5/R6 tickets extend that model (or its versioned successor) with
the validated report, valuation, recommendation, committee dissent, risk, QA,
and release status. An external report is an input to that packet, never a
packet alias.

`ArtifactEnvelope` remains the common durable-storage envelope. Bundle/report
domain models own their schema validation; the envelope owns artifact identity,
lineage, and payload integrity. Do not create a second generic artifact envelope
inside `agents.external`.

`WorkflowState` and its repositories alone own transitions. Large bodies and
raw completions remain outside state. State records bundle/report artifact IDs,
checksums, schema versions, route, validation outcome, and current status.

### Authority boundary

Deterministic EventLens code is authoritative for:

- event identity, timestamps, cutoff eligibility, provider provenance, and
  historical-card eligibility;
- canonical metrics, units, periods, arithmetic, scenario probability totals,
  forecast/zone ordering, and checksum calculation;
- citation allowlists, recommendation vocabulary, state transitions, release
  vetoes, rendering, and persistence;
- the distinction between valuation-derived zones and separately labeled
  mechanical guardrails.

The agent is authoritative only for bounded semantic judgments after validation:
claim synthesis, materiality and implications, company-specific thesis and
counter-thesis, scenario narrative, recommendation proposal, uncertainty, and
abstention explanations. A report may reference canonical metrics by immutable
ID but must not submit replacement metric values as facts.

## R1 interface freeze

All new Pydantic models are frozen, use `extra="forbid"`, normalize aware
timestamps to UTC, reject duplicate IDs, and expose a versioned JSON schema.
Schema version `1.0` accepts only `1.0`; schema evolution requires an explicit
migration or a new validator.

### `ExternalAgentBundle`

The version 1 bundle must contain these logical groups (field names may only
change before MVP-102 review):

- identity: `schema_version`, `bundle_id`, `session_id`, `event_id`, `ticker`,
  `route`, and bundle revision;
- temporal boundary: `information_cutoff_at`, target release metadata, and
  build timestamp;
- immutable facts: typed canonical metric references/values with metric ID,
  value, unit, period/as-of, calculation version, and provenance IDs;
- allowed evidence: stable source/passage IDs, evidence kind, bounded retained
  text or typed summary, publication/availability timestamps, content hash,
  rights/retention status, and provenance reference;
- historical context: prior-card ID, event ID/date, knowledge cutoff, checksum,
  readable summary/key messages, and sample/regime caveats;
- explicit missingness/warnings and unavailable stages;
- allowed skill/tool instructions with versions and forbidden behaviors;
- `content_sha256`, calculated over canonical JSON excluding the checksum field.

Canonical serialization uses Pydantic JSON-mode values followed by UTF-8 JSON
with sorted keys, compact separators, `ensure_ascii=False`, and `allow_nan=False`.
Timestamps are UTC and Decimals serialize as decimal strings. Lists whose order
is not semantically meaningful are sorted before model construction. Callers
must supply the build timestamp; checksum tests must not depend on wall-clock
defaults.

### `ExternalAgentReport` (legacy version 1)

The version 1 report must bind to exactly one bundle using `bundle_id`, bundle
revision, `bundle_sha256`, `session_id`, and `event_id`. It includes:

- report identity, schema version, generated timestamp, and producer/usage
  receipt without secrets or raw private prompts;
- evidence-backed claims with unique claim IDs and citations to exported
  source/passage IDs;
- bull/base/bear scenarios with probabilities, assumptions, cited evidence,
  and any canonical metric IDs used;
- signed and absolute reaction forecasts with horizon and uncertainty;
- one of `BUY`, `DONT_BUY`, `HOLD`, `TRIM`, or `SELL`, conviction, thesis,
  counter-thesis, invalidations, contradictory/supporting evidence IDs, missing
  evidence, and conditions that change the action;
- zero or more valuation zones with method/basis, currency, lower/upper bounds,
  assumption/calculation references, and citations;
- typed abstentions and warnings.

These five labels describe only the immutable version 1 reader/writer. New runs
must use ADR 0010's versioned user-defined policy contract or emit `no_policy`;
they must not silently map these labels into a default policy.

The display label for `DONT_BUY` may be “DON'T BUY”; serialized vocabulary stays
ASCII and stable. Precise valuation zones are optional and require a declared
valuation basis. Mechanical 90%/110%/125% guardrails are never report valuation
zones.

### Import validation order

The shared validator used by explicit import and DeepSeek must fail closed in
this order and return typed, secret-safe issues:

1. parse JSON with duplicate-key detection and apply exact schema version;
2. verify session/event/bundle identity and checksum against the paused state;
3. reject unknown/duplicate claim, scenario, citation, metric, and zone IDs;
4. resolve every citation against the bundle allowlist and reject evidence
   whose availability exceeds the cutoff;
5. ensure canonical metric references exist and reject submitted replacements
   or changed values/units/periods;
6. enforce action vocabulary, finite Decimal values, scenario probability
   total, signed/absolute forecast consistency, and price-zone ordering;
7. require valuation basis/calculation references for precise zones and keep
   mechanical guardrails distinct;
8. verify required support, contradiction, missingness, and abstention fields;
9. write the validated report atomically, record one audit event, and advance
   only the exact `awaiting_external_agent` session.

A rejected import writes no validated report and does not advance state.
Reimporting byte-identical valid output is idempotent; a different report for an
already-consumed bundle is a conflict, not a new revision.

## Ticket-level implementation guidance

### MVP-101

Expose a typed `external`/`deepseek` selector derived only from
`external_agent_enabled`. Test default true, accepted Pydantic boolean spellings,
invalid values, environment-over-file precedence, external readiness without a
model key, DeepSeek readiness with/without its key, and secret-safe output.

### MVP-102

Add the versioned models in `src/eventlens/agents/external.py` and export their
public surface from `eventlens.agents`. Generate a JSON schema under `schemas/`
from the same model. Keep R1 models separate from `AgentReportDraft`, whose
no-advice rule is intentionally incompatible. Test round trips, strict extras,
version rejection, UTC/Decimal encoding, duplicate IDs, probability/zone
invariants, and checksum stability.

### MVP-103

Create an assembler outside provider and UI code. It consumes normalized typed
inputs and returns a fully sorted bundle; it does not fetch data. Fail on
post-cutoff evidence/target outcomes and allow only approved bounded passage
content. Test a recursive secret/path scan of exported JSON and byte equality
from permuted equivalent inputs.

### MVP-104

Add an explicit graph stage `awaiting_external_agent` and persist the bundle
artifact reference/checksum before exposing the pause. The CLI only assembles,
atomically exports JSON/checksum, and invokes the graph transition. Repeated
export for the same revision returns the same artifact. Instrument tests so any
model/network invocation fails the test.

### MVP-105

Make import a compare-and-set operation against the paused session and bundle
checksum. Run the validation order above before persistence or transition. Do
not accept a report for a completed, different, or superseded session. Emit one
typed audit result for success or rejection without copying report prose into
the audit log.

### MVP-106

Adapt DeepSeek to produce `ExternalAgentReport`, not `AgentReportDraft`. Reuse
the exact bundle serializer and import validator. A bounded repair may address
syntax/schema failure, but it cannot expand evidence or change the bundle. No
Moonshot, fixture, or external fallback is permitted on this route.

### MVP-107

Fixtures must include one canonical valid bundle/report pair and small invalid
reports that each isolate one validator code (stale checksum, unknown citation,
post-cutoff citation, altered metric, bad probability, bad action, unsupported
zone, duplicate ID). Operator instructions receive only bundle JSON, the
generated report schema, and a stable prompt; no repository/chat context is
required.

## Existing-code conflicts to resolve during R1

1. `AgentReportDraft` rejects action words and cannot represent scenarios,
   valuation, or recommendations. It must not be reused or subclassed for R1.
2. `product.ask.run_research_ask` currently invokes the selected reasoning
   adapter directly and ignores `external_agent_enabled`. MVP-104/106 must route
   this path through export/pause/import or explicit DeepSeek.
3. `WorkflowStage` lacks `awaiting_external_agent`, and `WorkflowState` lacks
   immutable bundle/report references. MVP-104 owns that change.
4. `persistence.memory_models.RecommendationAction` omits `HOLD`. R1 uses the
   complete vocabulary, and the persistence/packet enum must converge before
   R6/R7 persistence rather than silently coercing HOLD.
5. The existing `DecisionPacket` has committee provenance and dissent but no
   refined recommendation, valuation, QA result, or release state. MVP-505 owns
   that versioned extension; R1 must not overload unrelated fields.
6. `ResearchAskResult.reasoning` and `locked_replay` are loose dictionaries and
   the DEMO path still contains mechanical decision language. They are legacy
   presentation shapes, not the R1 canonical contract.
7. The current OpenAI-compatible reasoning parser is hard-wired to
   `AgentReportDraft`. DeepSeek parity therefore requires a report-specific
   parser/adapter boundary rather than only changing its prompt.

## Consequences

- External and DeepSeek orchestration differ, but their semantic artifact and
  deterministic validation do not.
- A user can inspect and transport bundles without granting EventLens authority
  to call an external agent.
- Existing report-first and committee code remains usable while R1 introduces a
  distinct, stricter contract.
- UI and CLI can explain a paused or rejected run from typed state instead of
  guessing from files.

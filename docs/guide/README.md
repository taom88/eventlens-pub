# EventLens: start here

This guide is for someone arriving at the repository without prior context. It
explains the product’s purpose, how to trace a run, which decisions shape the
implementation, and where the current preview stops.

## In one paragraph

EventLens is a research workflow for company events, especially earnings. It
helps an analyst build a time-bounded record from eligible prior context and
source-backed evidence, calculate reproducible metrics, and optionally
request a bounded model interpretation. EventLens keeps inputs, calculations,
interpretation, and review state distinguishable so a reader can retrace what
the system knew and how it got to an output. It is research software—not a
broker, automated trading system, or complete investment-decision service.

## The mental model

Think of an EventLens run as an **auditable folder of typed records with a
workflow around it**, rather than a single prompt that returns an answer.

| Record or concept | What it means |
|---|---|
| Target event | The specific company event being analyzed, with issuer, fiscal period, schedule, and identity |
| Information cutoff | The latest time evidence may have been available for the pre-event analysis |
| Prior context card | An approved, readable record about an earlier event for the same company, with its own knowledge cutoff and lineage |
| Evidence receipt | Source and retrieval metadata that describe where an input came from, when it was available, and how it may be used |
| Research evidence bundle | A validated, typed input for a research request, including required identity, context, optional evidence, and explicit missingness |
| Deterministic calculation | A code-owned numerical result, such as an event-study statistic, with its sample and assumptions preserved |
| Agent bundle/report | A bounded request sent to an external agent, and its proposed semantic interpretation returned for validation |
| Product session | The durable sequence of status revisions and artifact references for one run |

The [product model](product-model.md) walks through how these records relate.

## How to explore the project

### If you want to try EventLens

Run:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
eventlens demo
```

Then follow the [D0 fixture runbook](../demo-runbook.md). It shows the
Streamlit screens, the external-agent pause, explicit report import, and
session restart. The fixture is synthetic and makes zero provider or model
calls.

### If you want to understand the product

Read these in order:

1. [Product model](product-model.md) — workflow, evidence boundary, and routes.
2. [Product decisions](product-decisions.md) — why the boundaries exist and
   what they mean for users and contributors.
3. [Implementation status](implementation-status.md) — supported path,
   separate live steps, and unfinished lifecycle stages.
4. [Decision records](../decisions/README.md) — what the numbered records
   capture, how their statuses work, and when they may be historical.

### If you want to understand or change the code

Read the [repository map](repository-map.md), then trace one concept through
its schema, service, adapter, and test. For example, follow the synthetic
fixture from `fixtures/d0/demo/` through
`src/eventlens/product/fixture_replay.py`, the product-session service, and
`tests/integration/test_d0_fixture_replay.py`.

## The user-facing journey

```mermaid
sequenceDiagram
    actor Analyst
    participant Adapter as CLI or Streamlit
    participant Session as ProductSessionFacade
    participant Evidence as Fixture or validated evidence
    participant Agent as External agent / selected model
    Analyst->>Adapter: Submit a research request
    Adapter->>Session: Create or resume a product session
    Session->>Evidence: Resolve input and validate cutoff/context
    Evidence-->>Session: Typed, provenance-linked facts or a typed blocked state
    Session-->>Adapter: Status, artifact references, and next action
    opt Interpretation requested
        Session-->>Analyst: Export checksum-verified bundle; pause by default
        Analyst->>Agent: Run the bounded prompt outside EventLens
        Agent-->>Analyst: Return structured report
        Analyst->>Adapter: Explicitly import report
        Adapter->>Session: Validate identities, citations, cutoffs, and schema
        Session-->>Analyst: Validated revision or a safe validation error
    end
```

The synthetic demo exercises a fixture replay of this handoff, including an
explicit import of a checked-in synthetic report. A live run is a different
path. Live collection has its own manifest, provider configuration, source
rights, budget, timing, and explicit authorization requirements.

## What to trust—and what not to infer

- Trust a numeric field only with its unit, period, method, availability time,
  provenance, and any stated limitations.
- Treat a model claim as interpretation until it is validated against the
  exported sources and canonical facts.
- Treat `null` with a reason as missing information, not as an invitation to
  infer or substitute a value.
- Treat fixture success as proof that the packaged path works for that fixture,
  not proof that live providers, issuer data, or model quality have been
  validated.
- Treat a passing evaluation test as evidence about the tested contract, not a
  blanket measure of research quality or a causal claim about performance.
- Treat provider terms and source rights as operator responsibilities; a stored
  URL, receipt, or hash does not grant redistribution rights.

## Canonical reference pages

| Question | Start with |
|---|---|
| What is the current architecture? | [Architecture](../architecture.md) |
| How does the local analyst interface behave? | [Web interface](../web-interface.md) |
| How is an agent bundle operated? | [External-agent contract](../external-agent-operator.md) |
| What does the live-data setup require? | [Live-provider setup](../public-release/live-provider-setup.md) |
| How are sources and provider routes handled? | [Provider strategy](../provider-strategy.md) |
| What does storage keep? | [Durable storage](../storage.md) and [Supabase setup](../supabase-setup.md) |
| What does evaluation currently establish? | [Learning and evaluation](../learning-and-evaluation.md) and [implementation status](implementation-status.md) |
| What is included in this public source preview? | [Public preview notes](../public-release/README.md) and [acceptance record](../public-release/acceptance-note.md) |

The product claims and design explanations in this guide describe the code and
documents in this public repository. They do not promise a future release date
or silently promote a planned capability to an implemented one.

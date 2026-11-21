# Decision records

This folder contains selected **architecture decision records (ADRs)**. An
ADR captures a consequential design choice: the context that prompted it, the
chosen boundary, and the trade-offs or follow-up work that choice creates.
These records help explain *why* parts of EventLens are shaped the way they
are.

## What these records are—and are not

An ADR is design history and rationale. It is not a user manual, a ticket
queue, a complete product specification, or proof that every described
capability is implemented today. Some records describe a compatibility slice,
an accepted design whose production work is still pending, or a contract that
later work has replaced in part.

For current behavior, check the implementation, its tests, and the linked
implementation-status or operator documentation. For current product scope
and priorities, check the active specs and tickets. When a record and current
code differ, do not infer that the older prose silently changes runtime
behavior; trace the current contract and its tests.

## Records in this public snapshot

| Record | Decision in plain language | How to read its status |
|---|---|---|
| [ADR 0001: Vibe-Trading boundary](0001-research-engine-boundary.md) | Keep the optional Vibe-Trading integration behind an EventLens-owned protocol, so upstream changes do not own core calculations or workflow state. | The record says the compatibility slice exists while production transport validation remains pending. |
| [Decision 0002: defer follow-up investigator](0002-defer-deepagent-follow-up-investigator.md) | Keep follow-up research bounded to approved evidence and defer a tool-using investigator until the core live workflow is more stable. | This is an accepted post-MVP design decision; the investigator described there is future work. |
| [ADR 0005: external-agent contracts](0005-external-agent-routing-and-packet-contracts.md) | Define the handoff between EventLens and an external or selected in-process agent, including immutable inputs and report validation. | Read this as a historical/compatibility record. Its own introduction says later ADRs supersede parts of it, and it explicitly labels the fixed action labels as legacy. Those later ADR files are not included in this public snapshot, so use current schemas and tests for the active contract. |

The numbering has gaps because this public source preview includes only a
selected subset of the project’s internal decision history. A missing number
does not imply a missing runtime feature or a task for a new contributor.

## When to consult an ADR

- Before changing an interface or ownership boundary described by the record.
- When code appears more complicated than expected and the rationale is not
  obvious from the implementation.
- When you need to tell an intentional constraint from an accidental
  limitation.
- Before treating a documented future direction as a supported feature.

ADRs do not replace the [product decisions guide](../guide/product-decisions.md),
[product model](../guide/product-model.md),
[implementation status](../guide/implementation-status.md), or the current
schemas and tests. The [repository map](../guide/repository-map.md) helps find
those implementation sources.

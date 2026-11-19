# ADR 0001: isolate Vibe-Trading behind an EventLens protocol

- Status: accepted for the compatibility slice; production transport pending a real LLM run
- Date: 2026-07-18

## Decision

All Vibe-Trading interaction will pass through a transport-neutral
`VibeTradingResearchEngine` protocol owned by EventLens. Domain calculations,
graph nodes, UI components, and artifact renderers will not call or import
Vibe-Trading directly.

The compatibility slice uses the official CLI JSON surface behind the protocol.
The executable/version preflight and no-key market-data path have passed. The
pinned CLI is allowed only for post-lock supporting research because its tool
registry cannot enforce the PRD's pre-lock boundaries. One real LLM-backed JSON
run plus a restricted REST/SSE or other sandboxed transport remain required
before any Vibe adapter is declared production-intended for the primary flow.

## Rationale

- Vibe-Trading is required product functionality but a separately evolving
  upstream project.
- EventLens requires stronger run/artifact provenance than a prose-only wrapper.
- Fixture replay and CI need mock/cached implementations of the same contract.
- Canonical financial calculations must remain owned and validated by EventLens.

## Consequences

- Adapter schemas and compatibility fixtures become part of the public internal
  boundary and require versioning.
- Some upstream capabilities may be unavailable until explicitly mapped.
- Transport-specific run IDs, streaming, cancellation, and artifacts are
  normalized rather than leaked into workflow state.
- A Vibe-Trading upgrade requires compatibility tests before adoption.

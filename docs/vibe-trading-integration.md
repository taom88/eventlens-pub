# Vibe-Trading integration contract

## Goal

Provide a stable EventLens boundary around Vibe-Trading while preserving run
provenance, artifacts, warnings, and errors. Vibe-Trading is optional per run;
mocked or cached output supports deterministic tests and demos.

## Audited upstream surface

As checked against the official HKUDS repository on 2026-07-25:

- Python 3.11+ and package `vibe-trading-ai`.
- EventLens pins and locally tests `vibe-trading-ai==0.1.11`.
- Official `main` reports `0.1.12`; GitHub Releases still marks `0.1.8` as
  latest. EventLens therefore did not silently upgrade the local dependency.
- Script-friendly `vibe-trading run --json` execution with the prompt on stdin.
- A local FastAPI service with sessions, messages, run lookup, and SSE events.
- An MCP server exposing 54 tools. Current official documentation supports
  stdio and Streamable HTTP (`http://127.0.0.1:8900/mcp`); legacy SSE is
  deprecated.
- Persistent memory, reports, run artifacts, and research-goal concepts.

References:

- <https://github.com/HKUDS/Vibe-Trading>
- <https://github.com/HKUDS/Vibe-Trading/blob/main/pyproject.toml>

Upstream is moving quickly. EventLens must pin and record the tested upstream
version/commit rather than assume `main` is stable.

## EventLens as an MCP client

EventLens neither imports nor copies Vibe internals. Its typed evidence client
uses only MCP initialization, discovery, and tool calls. Isolated local stdio
is the default. Optional Streamable HTTP connects to a separately running
official server; non-loopback endpoints require HTTPS and a distinct auth key.

The pre-lock allowlist is `get_market_data`, `get_stock_news`,
`get_sec_filings`, `get_financial_statements`, `get_stock_profile`, and
`get_research_reports`. File mutation, `trading_*`, `run_swarm`, shell-capable
tools, and unrestricted URL/web retrieval are rejected before upstream access.

Connection/tool timeouts, retries, cancellation, exact arguments, idempotency,
and checksum-addressed artifacts are EventLens-owned. Direct DeepSeek or
Moonshot/Kimi calls perform cited interpretation after deterministic collection.

EventLens does not import Vibe-Trading internals into domain or graph code. The
MCP subprocess is accessed through its public protocol and wrapped in the same
typed research-engine contract as the mock transport.

The earlier `VibeTradingCliEngine` Python API was removed before the public MVP
because it had no production caller and could not enforce EventLens's pre-lock
tool/cutoff allowlist. This is an intentional pre-1.0 API break: use
`VibeMcpEvidenceClient` for the supported restricted evidence boundary. No
deprecated CLI shim remains because it would preserve an unsafe parallel path.

## Protocol sketch

```python
class VibeTradingResearchEngine(Protocol):
    async def check_availability(self) -> Availability: ...
    async def run(self, request: VibeTradingRunRequest) -> VibeTradingRunResult: ...
    async def get_run(self, run_id: str) -> VibeTradingRunResult: ...
    async def get_artifact(self, reference: ArtifactReference) -> bytes: ...
```

The real, mock, and cached adapters implement the same protocol.

## Request contract

Required fields:

- Schema version.
- EventLens session and event IDs.
- Ticker, event date, fiscal quarter when known.
- Research mode and run purpose.
- Focused research question.
- Allowed data sources and tools.
- Expected structured output format.
- Pre-lock/post-lock information boundary.
- Idempotency key.
- Audit correlation ID.
- Timeout and cancellation policy.

Prompts should tell Vibe-Trading that its results are supporting research, not
canonical EventLens calculations or investment advice.

## Result contract

Required fields:

- Status: `succeeded`, `partial`, `failed`, `timed_out`, or `cancelled`.
- EventLens correlation fields.
- Vibe-Trading run/session IDs when available.
- Transport and upstream version/commit.
- Summary and structured payload, if available.
- Artifact references with media types and checksums.
- Data sources and tool trace references.
- Warnings and structured errors.
- Created/completed timestamps.
- Raw output reference and checksum.
- Origin: `live`, `cache`, `fixture`, or `mock`.

## Validation boundary

Vibe-Trading may supply research context, candidate facts, and research
artifacts. EventLens must:

1. Preserve the raw result or a checksum-addressed reference.
2. Validate the result schema.
3. Attribute every incorporated claim to the run and source.
4. Recompute canonical metrics from normalized provider inputs.
5. Keep upstream warnings attached to downstream artifacts.

Every evidence request carries ticker, event ID, session ID, information
cutoff, boundary, and idempotency key. Post-cutoff evidence is rejected.
Canonical-looking upstream fields remain attributed raw evidence and can never
populate EventLens metrics, dates, release times, sessions, or hypotheses.

## Credentials and operating modes

Credential classes remain separate: EventLens reasoning keys; Vibe's own
agent/CLI/swarm LLM configuration; EventLens market-data credentials; broker
connector credentials; and `EVENTLENS_VIBE_MCP_HTTP_AUTH_KEY` for remote MCP
authentication. EventLens never automatically forwards its Alpha Vantage,
Finnhub, SEC, or external reasoning secrets into Vibe.

Broad CLI/agent/swarm execution is outside the supported EventLens runtime.
`run_swarm` and other shell-capable tools remain blocked.

## Operator configuration

Local stdio starts `vibe-trading-mcp` automatically. A newer official server
can be started for Streamable HTTP with:

```bash
vibe-trading-mcp --transport http --host 127.0.0.1 --port 8900
```

Then configure `EVENTLENS_VIBE_MCP_TRANSPORT=streamable_http` and
`EVENTLENS_VIBE_MCP_HTTP_URL=http://127.0.0.1:8900/mcp`. Non-loopback endpoints
require HTTPS plus `EVENTLENS_VIBE_MCP_HTTP_AUTH_KEY`.

The adapter must reject or quarantine any upstream field that attempts to
overwrite reference spot, expected move, realized move, realization ratio,
event-window selection, or locked analyst input.

## Failure and degraded-mode behavior

| Condition | Required behavior |
| --- | --- |
| Executable/server unavailable | Mark unavailable; require analyst approval before fixture/hybrid continuation |
| Timeout | Return typed timeout with partial artifact references if safe |
| Malformed JSON/schema | Store raw checksum, return parse error, do not use summary |
| Missing run ID | Mark lineage incomplete and treat as degraded |
| Cached response | Show cache age and never label it as a fresh run |
| Upstream warning | Propagate to event packet, briefs, validation report, and audit |
| Canonical metric conflict | Retain as attributed research note; deterministic value wins |

## Compatibility spike checklist

- Install a pinned released version in an isolated environment.
- Capture `vibe-trading --version` and package metadata.
- Run one harmless DEMO earnings-research prompt with `--json`.
- Document stdout/stderr, exit codes, run IDs, schema, artifacts, and timeout
  behavior.
- Verify whether continuing a run/session is supported from the chosen surface.
- Verify path containment and artifact retrieval.
- Compare CLI and REST behavior for cancellation and streaming.
- Build golden success/failure samples without secrets.
- Record the final transport choice in an ADR.

The spike established that upstream v0.1.11 registers shell tools in its broad
CLI and cannot technically enforce EventLens source/tool/cutoff allowlists.
EventLens therefore removed its unused CLI adapter instead of shipping a
quarantined public API. The restricted MCP adapter supplies the intended
pre-lock evidence calls without exposing the shell-capable agent surface.

## Compatibility result

The local spike installed the released `vibe-trading-ai==0.1.11` package and
confirmed:

- `vibe-trading --version` reports `vibe-trading 0.1.11`.
- EventLens can find the executable beside its virtual-environment Python even
  when the environment has not been shell-activated.
- Vibe's no-key `DEMO.US` auto-route returned five daily bars for
  2026-07-13 through 2026-07-17 without Alpha Vantage or Finnhub credentials.
- EventLens's direct yfinance adapter returned the same five-session window
  without a key.
- `eventlens vibe-check` validates MCP transport and schemas without requiring
  unrelated LLM authentication; optional reasoning readiness is reported
  separately and secret values are never printed.
- `run --json` emitted metadata (`status`, `run_id`, `run_dir`, `reason`) rather
  than the final answer. Vibe's package-level trace location and full-prompt
  retention could not be redirected in v0.1.11; this is one reason EventLens
  did not retain the CLI adapter as a production transport.

Paid-model behavior still needs a harmless attributed smoke test after the user
configures DeepSeek or Moonshot/Kimi. CLI observations above are historical
spike evidence, not a supported executable workflow transport. Host IDE and
agent credentials are not forwarded into the MCP evidence client.

The installed 0.1.11 stdio server discovers the six EventLens evidence tools.
Official 0.1.12 `main` documents Streamable HTTP and the same tool names. That
remote transport is unit-tested with fake MCP callers but was not live-smoked
because `EVENTLENS_RUN_LIVE_VIBE_MCP_SMOKE` was not explicitly enabled.

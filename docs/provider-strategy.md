# Free-first live-data and key strategy

## Decision

Alpha Vantage and Finnhub are confirmed EventLens providers, but they are not
generic first choices for every request. The routing policy preserves their
useful free entitlements and avoids paid-only endpoints.

### Near-term versus historical routing rule

Use the provider with the strongest entitlement for the specific temporal
job; do not substitute one provider globally:

- For an upcoming event inside Finnhub's free one-month window, Finnhub is
  authoritative for the near-term event ID, date, and BMO/AMC timing. Its
  `free_tier_one_month` warning is expected and does not invalidate a future
  event returned inside that window.
- For prior quarters and historical context cards, Alpha Vantage's
  `EARNINGS` response supplies the longer earnings/EPS history. Finnhub must
  not be used to backfill older calendar history beyond its free entitlement.
- For a current event's consensus or estimate cross-check, Alpha Vantage may
  be called once after the Finnhub event is resolved; the receipts remain
  separate and disagreement is surfaced rather than silently merged.
- The composite event provider exists for broad historical-plus-near-term
  windows: Alpha Vantage supplies historical rows and Finnhub wins for the
  final near-term window. A narrow upcoming preflight may use Finnhub directly
  to avoid an unnecessary second calendar call.
- For news, Massive is primary for the synthetic acceptance fixtures;
  Alpha Vantage and Finnhub remain auditable fallbacks for manifests that name
  them. All are metadata/sentiment evidence unless separate rights approval exists.
  Finnhub provider summaries are withheld by default and receive an explicit
  retention status; callers must opt in only after confirming permission.

| Need | First source | Key/cost | EventLens policy |
| --- | --- | --- | --- |
| Adjusted US daily OHLCV | yfinance | No key | Canonical history source; Vibe may also use its free yfinance/Stooq/Yahoo chain. |
| Historical filing evidence | SEC EDGAR Item 2.02 8-K | No key | Evidence only. A filing date is never silently treated as the earnings event date. |
| Near-term earnings date and BMO/AMC timing | Finnhub | Confirmed free key | One request for a window of at most 31 days; cache past windows indefinitely. |
| Recent EPS surprise cross-check | Finnhub | Confirmed free key | Free plan exposes four quarters; do not expect a long event study from it. |
| Earnings history and EPS cross-check | Alpha Vantage | Confirmed free key | One response covers the ticker's quarterly history; cache for seven days. |
| Earnings transcript | Alpha Vantage | Confirmed free key | One call per ticker/quarter; immutable transcript responses are cached indefinitely. |
| Historical pre-event option chain | Curated fixture | No key | Both confirmed providers' relevant historical options coverage is paid or unsuitable. Never silently substitute current quotes. |
| Post-lock research/reasoning | Vibe-Trading + DeepSeek or Moonshot/Kimi | One LLM key, or local Ollama | Supporting research only; EventLens recomputes all canonical metrics. Pinned CLI v0.1.11 is refused for pre-lock work. |

## Confirmed free-tier boundaries

Alpha Vantage's standard allowance is 25 requests per day. EventLens caps
itself at 20, leaving five calls for manual checks or other tools. Free
`TIME_SERIES_DAILY` returns only the latest 100 raw points; full output,
adjusted daily history, and historical options are premium. EventLens therefore
uses the free daily endpoint only as a visibly degraded diagnostic fallback,
not for the adjusted-close realized-volatility calculation.

Finnhub's free plan currently allows 60 calls per minute, one month of US
earnings-calendar history/updates, and four quarters of EPS surprises. Stock
candles and earnings transcripts are premium. EventLens sets a conservative
50-call internal daily budget, spaces live calls by at least 1.05 seconds, and
rejects calendar history older than the free one-month entitlement. It uses
Finnhub as a near-term calendar cross-check. Alpha Vantage `reportTime` is
normalized when present; unrecognized or missing timing remains visibly
unknown.

SEC EDGAR needs no API key, but requests must include an identifying contact
User-Agent and respect SEC fair access. EventLens does not use SEC filing dates
as release dates.

## How quota is protected

1. A secret-free request fingerprint is checked against the disk cache before
   a quota unit is reserved.
2. Cache and quota-ledger writes are atomic and process-locked on POSIX, so two
   local EventLens runs do not intentionally spend the same call.
3. API keys are excluded from fingerprints, provenance, cache files, CLI
   status, and normalized errors.
4. Alpha Vantage transcripts and past Finnhub calendar windows are cached
   indefinitely; Alpha Vantage earnings history is cached for seven days.
5. Paid endpoints are not integrated in the free-first MVP and fail before an
   HTTP request.
6. Alpha Vantage and Finnhub keys are not passed to Vibe-Trading by default.
   This keeps Vibe's automatic fallback on no-key sources and all keyed usage
   inside EventLens's quota ledger. Set
   `EVENTLENS_ALLOW_VIBE_KEYED_MARKET_DATA=true` only for a deliberate spike.
7. Corrupt quota/rate ledgers fail closed instead of silently resetting usage.

The internal ledger is a safety budget, not a provider billing meter. Provider
dashboard usage remains the final authority.

## Vibe MCP evidence routing

Vibe-Trading is supporting evidence, not a canonical provider. EventLens
connects as an MCP client over isolated stdio by default or optional Streamable
HTTP. Pre-lock calls are restricted to six read-only evidence tools and retain
the exact tool/arguments, transport, timestamps, origin, warnings, and
checksum-addressed raw result.

EventLens Alpha Vantage, Finnhub, SEC, and reasoning credentials are not
forwarded into Vibe. Public Vibe market-data routes can work without keys.
Vibe agent/swarm reasoning uses Vibe's own LLM configuration, broker tools use
connector credentials, and remote MCP authentication uses the separate
`EVENTLENS_VIBE_MCP_HTTP_AUTH_KEY`.

## Keys to obtain

Copy `.env.example` to `.env`; `.env` is gitignored. For the MVP, obtain:

1. `ALPHAVANTAGE_API_KEY` — required and free.
2. `FINNHUB_API_KEY` — required and free.
3. Exactly one reasoning choice:
   - `DEEPSEEK_API_KEY` for the recommended pay-as-you-go Vibe path, or
   - `MOONSHOT_API_KEY` for Kimi, or
   - no LLM API key when using a local Ollama model.

Also replace `EVENTLENS_SEC_USER_AGENT` with a real application/contact email.
It is an identity header, not a secret.

Massive is approved for the bounded ticker-news gap. Do not obtain FMP, Tiingo,
Intrinio, or FRED keys for the current MVP; they remain placeholders for a
later, evidence-based gap decision.

Run these preflights without exposing values:

```bash
eventlens config-check
eventlens provider-plan
eventlens vibe-check
```

## Source references checked on 2026-07-18

- Alpha Vantage documentation: <https://www.alphavantage.co/documentation/>
- Alpha Vantage standard limit: <https://www.alphavantage.co/premium/>
- Finnhub API documentation: <https://finnhub.io/docs/api>
- Finnhub plan matrix: <https://finnhub.io/pricing>
- SEC fair-access limit: <https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits>
- Vibe-Trading repository: <https://github.com/HKUDS/Vibe-Trading>
- Vibe-Trading environment template: <https://github.com/HKUDS/Vibe-Trading/blob/main/agent/.env.example>

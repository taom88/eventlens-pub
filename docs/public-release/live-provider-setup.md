# Using your own provider credentials

The bundled demo is synthetic, offline, and makes no provider or model calls.
Provider-backed collection is a separate, explicitly authorized workflow. The
fixtures under `docs/acceptance/` and
`tests/fixtures/live_run_configs/` are structural examples only: they use the
DEMO ticker, `example.com` URLs, an expired event date, and collection disabled.
Do not run them as live inputs.

## 1. Install and configure credentials

Install the provider extras used for market history, then copy the environment
template:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[free-data]'
cp .env.example .env
```

Put credentials only in the ignored `.env` file or the process environment. Do
not put them in manifests, run configs, source, receipts, or Git. Use only keys
for providers selected by your run config and event manifest:

- `ALPHAVANTAGE_API_KEY` for earnings history, estimates/transcripts, and news
  when the manifest selects Alpha Vantage.
- `FINNHUB_API_KEY` for the near-term event and surprise cross-checks selected by
  the manifest.
- `MASSIVE_API_KEY` only when the manifest selects Massive for news.
- `EVENTLENS_SEC_USER_AGENT` as a real contact identity for SEC requests; it is
  not a credential.

Live evidence collection does not require an LLM key. `DEEPSEEK_API_KEY`,
`MOONSHOT_API_KEY`, and Vibe settings are for separate reasoning/research paths;
the evidence collection command does not use them. Check readiness without
printing secret values:

```bash
eventlens config-check
eventlens provider-plan
```

## 2. Prepare a current event contract

Copy the synthetic acceptance manifest and run config to new files and replace
all DEMO values with your own verified event data. Keep the contract fields
consistent: ticker, company name, ontology ID, fiscal period, event date and
timing, issuer-confirmed source URLs, information cutoff, provider window,
benchmark, request budget, and required environment-variable names. The run
config's `manifest_relative_path`, ticker, event date, fiscal period, and
cutoff must match the manifest exactly. Recompute the manifest sidecar SHA-256
after each edit; the loader fails closed when the checksum differs. Write the
sidecar as the hash alone (no filename):

```bash
python - <<'PY'
import hashlib
from pathlib import Path

manifest = Path("docs/acceptance/my-event.json")
sidecar = manifest.with_suffix(manifest.suffix + ".sha256")
sidecar.write_text(hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n")
PY
```

Choose the cutoff before the earliest information you want to exclude. Keep it
strictly before the expected results release and conference call. Resolve and
verify the current event schedule from the issuer's own sources; the collection
step fetches the two issuer timing URLs again and checks that they still support
the frozen schedule. The two source URLs must be real, sorted, and unique.

The run config must declare `data_mode: "live"`. Leave
`live_provider_run_authorized` false until an owner has reviewed the source
manifest, cutoff, call budget, data rights, and actual provider costs. Setting it
true permits collection only on the cutoff's local calendar date and strictly
before the cutoff. It does not bypass the time gates.

## 3. Collect and assemble

From the repository root, use paths to your own files:

```bash
.venv/bin/python scripts/run_live_evidence.py collect \
  --manifest docs/acceptance/my-event.json \
  --run-config tests/fixtures/live_run_configs/my-event.json \
  --env-file .env

.venv/bin/python scripts/run_live_evidence.py index-news \
  --manifest docs/acceptance/my-event.json \
  --run-config tests/fixtures/live_run_configs/my-event.json

.venv/bin/python scripts/run_live_evidence.py assemble \
  --manifest docs/acceptance/my-event.json \
  --run-config tests/fixtures/live_run_configs/my-event.json
```

`collect` makes real provider requests, revalidates issuer timing, and writes
private receipts under the configured output root. Review the receipt statuses
and failures before running `index-news` and `assemble`. Assembly is allowed only
after the information cutoff and before the expected results release. The
`replay` phase is for explicitly retrospective processing of already retained
receipts; it does not collect live evidence.

The automated suite uses mocked providers and verifies credential handling,
cutoff checks, and fail-closed behavior. It does not certify current provider
availability, API entitlements, prices, or a live run with your account. Provider
quotas and terms apply, and every output still requires human review.

# EventLens — Alpha source preview

EventLens is a research workflow for preparing, reviewing, and preserving
evidence-based analysis around scheduled company events. It combines typed
evidence, cutoff-aware historical context, deterministic calculations, and an
optional external-agent handoff. EventLens does not execute trades. This
repository is an alpha source preview, not a completed production research
service or investment product.

Maintainer: [@taom88](https://github.com/taom88). For project questions and issues, use the GitHub issue tracker.

The product is designed for individual researchers and small research teams
who need a repeatable record of what was known, what was inferred, and what
remains uncertain before an event. JSON artifacts are canonical; readable
Markdown and the Streamlit interface make the work easier to review.

## Preview limits

The synthetic demo is the supported public smoke path. Provider-backed research
requires separately configured services and credentials. The end-to-end live
research, review, and publishing lifecycle is incomplete; outputs require human
review and must not be treated as investment advice. See [the preview limits and
synthetic output](docs/public-release/README.md) and [the live provider setup
guide](docs/public-release/live-provider-setup.md) before using the code.

## Quick start

Requires Python 3.11 or later. The bundled demonstration is synthetic and runs
without API keys, external services, or network access.

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
eventlens demo
```

To open the local interface:

```bash
eventlens app
```

The demo uses a manifest-checked fixture containing three readable historical
context cards, a deterministic baseline, and a validated external-agent
handoff. Fixture data is clearly labeled and is not actual company history or
investment guidance.

## How it works

1. Resolve the requested event and its information cutoff.
2. Gather typed evidence and cutoff-eligible prior context.
3. Calculate deterministic metrics and preserve missingness and provenance.
4. Export a typed bundle for review by an external agent, or use a configured
   reasoning adapter when explicitly selected.
5. Validate imported claims and citations before persisting the result.

External-agent mode is enabled by default. EventLens exports the immutable
bundle and pauses for an explicit validated import; it never invokes another
agent automatically. Setting `EVENTLENS_EXTERNAL_AGENT_ENABLED=false` selects
the configured DeepSeek adapter through the same typed validation contract.

## Configuration

The neutral fixture demo needs no configuration. For provider-backed research,
copy `.env.example` to `.env` and fill only the credentials for providers you
choose to use. Keep `.env` local and never include credentials in exported
bundles. SQLite is local-only for demos and tests. Every deployed instance
needs a Supabase project owned by its deployment owner for durable state; this
repository has no shared project or credentials. Configure a unique owner and
workspace identifier for each deployment. See
[storage configuration](docs/storage.md) and
[Supabase setup](docs/supabase-setup.md).

Useful commands include:

```bash
eventlens config-check
eventlens provider-plan
eventlens research-ask --question "Prepare research for the next company event" \
  --ticker SYMBOL --evidence-bundle evidence.json
```

See [the operator guide](docs/external-agent-operator.md),
[the demo runbook](docs/demo-runbook.md), and [the architecture](docs/architecture.md)
for workflow details. The public source snapshot documents its boundaries in
[docs/public-release/README.md](docs/public-release/README.md).

## Development

```bash
pip install -e '.[dev]'
python -m pytest -q
python -m compileall -q src tests
```

The public package includes its schemas, neutral demonstration fixture, and
operator instructions. The repository contains additional test fixtures and
development documentation that are not required to run the demo.

# Public source preview

This is a source preview of EventLens, an alpha research workflow. It is useful
for inspecting the architecture and running the bundled synthetic demo. It is not
a completed production research service, and the repository does not publish
investment recommendations.

New readers can start with the [EventLens guide](../guide/README.md), which
explains the product model, design decisions, current implementation boundary,
and repository map.

## Supported smoke path

Install the package with Python 3.11 or later and run:

```bash
python -m venv .venv
. .venv/bin/activate
pip install .
eventlens demo
```

The smoke command is deterministic and makes no provider or model calls. Its
output is a synthetic fixture, not historical issuer data.

## Synthetic demo output

```json
{
  "status": "validated",
  "fixture_id": "d0-demo-2026q3",
  "session_id": "d0-fixture-demo-2026q3",
  "context_cards": 3,
  "provider_calls": 0,
  "model_calls": 0,
  "notice": "Synthetic demonstration data; not actual company history or investment guidance."
}
```

For the provider-backed path, see [Using your own provider credentials](live-provider-setup.md).

## Known limits

- The complete live research, review, and publishing lifecycle is not implemented.
- Provider-backed runs require independent provider accounts, credentials, current
  terms, and owner review. The demo does not verify those services.
- External-agent handoff is explicit. EventLens does not launch an external agent automatically.
- The context-card evaluation fixture is synthetic and has no human quality
  scores; it only smoke-tests the evaluator.
- Dependency license texts and notice files were reconciled for the 205-row
  release-prep inventory. EventLens source and wheel do not bundle dependency
  code; any combined or container distribution needs its own target-platform
  notice bundle and review. See the [inventory](dependency-license-inventory.json),
  [review record](dependency-license-review.md), and [third-party
  notices](../../THIRD_PARTY_NOTICES.md).
- Automated release scans and tests do not establish rights to redistribute
  third-party materials or provide legal clearance.

This preview is research software, not investment advice. Review the [data
policy](../../DATA_POLICY.md), [privacy](../../PRIVACY.md), [disclaimer]
(../../DISCLAIMER.md), [security policy](../../SECURITY.md), and Apache-2.0
license before use or redistribution.

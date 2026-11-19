# Contributing

Contributions should preserve EventLens's research-only scope and its cutoff,
provenance, rights, and fail-closed boundaries.

- Keep changes focused and include tests for behavioral changes.
- Use synthetic or demonstrably redistributable fixtures only. Do not submit
  provider payloads, credentials, personal data, proprietary prompts, customer
  artifacts, or copied third-party implementations.
- Document changes to dependencies, network destinations, retention, or data
  handling.
- Run relevant tests, `python -m compileall -q src tests`, and
  `git diff --check` before opening a pull request.
- By submitting a contribution, you certify that you have the right to submit
  it and agree that it is licensed under Apache-2.0.

Describe user-visible changes, verification performed, and effects on schemas,
fixtures, data rights, or compatibility.

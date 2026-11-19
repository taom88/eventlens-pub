# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this
repository. Do not include credentials, proprietary data, or customer artifacts
in a report. If private reporting is unavailable, contact repository
maintainers through a private channel listed in the repository hosting service.

## Deployment assumptions

- Keep `.env`, provider keys, database service-role keys, and generated run
  artifacts outside version control.
- Use least-privilege credentials and deployment-specific persistence. Never
  expose a service-role key to a browser.
- Treat evidence text, imported JSON, and model output as untrusted input.
- Restrict outbound network destinations and external-agent capabilities.
- A hosted deployment must provide its own authentication, TLS, rate limiting,
  authorization, and tenant isolation.

EventLens validates schemas, checksums, cutoff timestamps, citations, and
allowed evidence references. Those checks do not make an untrusted host,
provider, model, or dependency safe.

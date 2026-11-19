# Data policy

## Repository and package contents

The repository and published package contain software, schemas, documentation,
and synthetic demonstration fixtures. They do not provide live market data,
issuer documents, news articles, transcripts, research reports, customer
artifacts, API keys, or a shared Supabase dataset.

## Operator responsibilities

An operator connecting a provider must have the right to access, process,
retain, and display its data. Provider terms control over EventLens defaults.
Configure the shortest practical retention, retain bounded excerpts only when
permitted, use summaries or metadata where required, and honor deletion or
attribution obligations.

Evidence provenance and rights metadata support governance; they do not grant
a license to source data. Inaccessible content must be labeled
`headline_only` and must not be represented as content-scored evidence.

Pull requests must not include proprietary, personal, credentialed, or live
provider data. Use synthetic or independently redistributable fixtures and
document their provenance.

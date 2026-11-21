# Data policy

## Repository and package contents

The repository and published package contain software, schemas, documentation,
and synthetic demonstration fixtures. The repository also contains one
curated public-source case study at
`docs/examples/nvda-fy2027-q3-pre-earnings-brief.md`. It consists of concise
paraphrases of linked NVIDIA investor disclosures and an SEC filing; it does
not include source-document text, provider output, third-party market-price
series, news articles, transcripts, customer artifacts, API keys, or a shared
Supabase dataset. The case study is retrospective and is not a validated live
research run. It contains a non-personal, historical directional analysis but
no formal Buy/Sell/Hold rating, position sizing, or order instruction.

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

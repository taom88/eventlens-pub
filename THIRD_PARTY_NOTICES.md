# Third-party notices

EventLens depends on third-party Python packages listed in `pyproject.toml`.
Those packages are not relicensed by this repository; their own licenses and
notices apply. The [resolved dependency/license inventory](docs/public-release/dependency-license-inventory.json)
records 205 runtime, development, and optional-extra packages from the release-prep
environment. A [platform-specific constraints file](docs/public-release/constraints-macos-arm64-py314.txt)
pins that environment for CPython 3.14.3 on macOS arm64. This environment is a
reproducibility aid, not a cross-platform lock.

Optional provider and model integrations connect to independently operated
services. Their names and API compatibility do not imply affiliation,
endorsement, or permission to redistribute their data.

The 205 resolved package rows (201 distinct distributions) have been
reconciled against installed license/notice texts or official upstream license
sources. The inventory records normalized license expressions, distribution
file paths, and SHA-256 evidence. EventLens source and wheel do not bundle
dependency code; no license incompatibility was identified for that boundary.
Any combined or container distribution must retain each dependency's license
and notice files and be reviewed for its target platform. This engineering
review is not legal clearance. See the [dependency license review record](docs/public-release/dependency-license-review.md).

No live third-party market, issuer, transcript, news, or research dataset is
included in this repository. Demonstration data is synthetic.

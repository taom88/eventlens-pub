# Public source snapshot acceptance note

This directory is a local source-preview candidate prepared for owner review. It
was initialized as a new Git repository with no remote and no private-repository
ancestry. Publication and repository visibility have not been changed. The intended
public maintainer account is [@taom88](https://github.com/taom88); no personal
email address is published in this snapshot.

## Included boundary

The snapshot contains the package, tests, schemas, synthetic acceptance and demo
data, operator/product documentation including a live-provider setup guide, CI,
release scans, license files, and the dependency/license inventory and review record. It omits private EL ticket history,
owner-specific planning, local caches, credentials, generated research, and
issuer-specific acceptance/evaluation artifacts.

## Verification performed

- Full suite: 677 passed, 1 skipped, 1 xfailed.
- Compile check: `python -m compileall -q src tests` passed.
- Public-release scan: passed for 328 text files; its documented limitations
  still apply.
- Context-card evaluator smoke: 1 synthetic case, 0 critical failures, 4
  missing human-review inputs. This is not a quality benchmark.
- Wheel build: passed; wheel installed into a clean target directory.
- Installed-wheel offline demo: validated, 3 synthetic cards, 0 provider calls,
  0 model calls.
- Massive connection smoke: one request through the snapshot adapter authenticated
  and parsed a live response for the synthetic `DEMO` ticker (0 articles). The API
  key was not printed or retained. This verifies endpoint reachability and news
  response parsing only; it does not verify other provider accounts or a full run.
- `git diff --check`: passed.

Private-repository commit identifiers in the retained synthetic E0 evaluation
record were replaced with zero placeholders; no private Git ancestry is present.

The tests and wheel verification used the release-prep Python 3.14.3 macOS arm64
environment. The repository CI matrix remains the cross-version check.

## Publication condition

The resolved dependency inventory covers 205 rows and 201 unique package
versions. Engineering reconciliation records 310 installed distribution
license/notice file hashes across 196 rows, one embedded source-file license,
and eight exact upstream references for releases whose installed wheels had no
named license file. The source snapshot and EventLens wheel do not bundle
third-party dependency packages; no incompatibility was identified for this
separate-package boundary. See the [dependency license review](dependency-license-review.md).

This is an engineering review, not legal advice or legal clearance. The
inventory is one macOS arm64 CPython 3.14.3 resolution, not a cross-platform
lockfile. A container or other combined distribution needs a fresh
platform-specific dependency inventory and required-notice review.

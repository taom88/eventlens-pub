# Dependency license and notice review

**Status:** engineering review complete for the EventLens source repository and
wheel. This is not legal advice or legal clearance.

## Scope and method

The inventory covers the exact CPython 3.14.3 macOS arm64 resolution for the
`runtime`, `dev`, `free-data`, and `vibe` groups declared in `pyproject.toml`.
It contains 205 package rows and 201 distinct name/version distributions. All
resolved versions were matched to the installed environment. The inventory
normalizes legacy package license labels into 25 license expressions or scoped
license descriptions, retains the original metadata labels, and records its
evidence per package row.

The installed distributions supplied 310 license/notice-named files across 196
rows; SHA-256 hashes identify each exact file. One additional row (`jsonpath`)
has the full MIT grant in a source-file header. Eight rows rely on official
upstream release/license references because their installed wheels contain no
license-named file. Those links are recorded in the inventory. No package row
remains without a license expression and evidence reference.

This approach cross-checks package metadata against distribution content or
official upstream release records. It does not treat metadata labels as a
substitute for license text, and it does not claim that a hash proves legal
compatibility.

## License families and compatibility

Most resolved rows use a single permissive license family: MIT (94 rows),
BSD-3-Clause (46), Apache-2.0 (32), PSF-2.0 (5), BSD-2-Clause (5), ISC (3),
MPL-2.0 (2), Unlicense (1), or MIT-CMU (1). One further MIT row (`wcwidth`)
has a separate Markus Kuhn permission notice. The remaining rows are
represented by the compound or scoped expressions in the machine-readable
inventory.

The five declared runtime dependencies use Apache-2.0, BSD-3-Clause, or MIT.
The dev and free-data groups do not add a license conflict for EventLens’s
current packaging boundary. The optional `vibe` group contains the nontrivial
cases below. Their own packages retain their license and notice files when
installed; EventLens does not copy their code into its source tree or wheel.

| Dependency | Reconciled terms and notice finding |
|---|---|
| `pyphen` | Code is offered under GPL-2.0-or-later, LGPL-2.1-or-later, or MPL-1.1. Included LibreOffice dictionaries have terms that vary by dictionary. Its wheel contains the three `COPYING` texts and a package notice. Preserve them if redistributing the dependency. [PyPI 0.17.2](https://pypi.org/project/pyphen/0.17.2/) |
| `orjson` | MPL-2.0 together with Apache-2.0 or MIT. The distribution has all three license files. [Installed license metadata](dependency-license-inventory.json) |
| `tqdm` | The installed `LICENCE` assigns MIT/MPL terms by file. Preserve the file-level notice. |
| `simplejson` | MIT or AFL-2.1; its installed `LICENSE.txt` includes both complete texts. |
| `regex` | Apache-2.0 additions with CNRI-Python terms for derived CPython portions. Both are represented in its installed license file. |
| `pypdfium2` | The wrapper is Apache-2.0 or BSD-3-Clause; documentation/examples are CC-BY-4.0. The macOS arm64 wheel also contains PDFium native code and a platform-specific `BUILD_LICENSES` set, including Apache, BSD, MIT, FreeType, and Unicode license material. Preserve that complete set when redistributing the dependency. [5.12.1 release](https://github.com/pypdfium2-team/pypdfium2/releases/tag/5.12.1), [upstream license guidance](https://github.com/pypdfium2-team/pypdfium2/blob/main/README.md) |
| `numpy` | Its installed wheel includes BSD-3-Clause, 0BSD, MIT, Zlib, and CC0-1.0 component license files. |
| `llvmlite` | BSD-2-Clause and Apache-2.0 with LLVM exception. |
| `aiohttp` | Apache-2.0 package license plus an MIT license for vendored `llhttp`. |
| `python-dateutil` | Distribution metadata says “Dual License” and lists BSD and Apache classifiers. Its installed `LICENSE` says Apache-2.0 applies after 2017-12-01 and to contributors who relicensed; older contributions retain their applicable notices. Treat this as contributor/file-scoped and preserve the complete license file. |
| `sniffio`, `uvloop` | Apache-2.0 and MIT license files are present. `sniffio` states an either/or choice for the package, with contributions under both; preserve both files. |
| `cryptography`, `packaging`, `ormsgpack` | Their installed metadata and files identify the stated Apache/BSD/MIT license alternatives. |
| `wcwidth` | MIT plus a separate Markus Kuhn permission notice in the same `LICENSE`; preserve both. |
| `vibe-trading-ai` | MIT package license plus a separate `NOTICE` and factor attribution/license documents, including Qlib Apache-2.0 attribution. Preserve these files if redistributing it. [upstream NOTICE](https://github.com/HKUDS/Vibe-Trading/blob/main/NOTICE) |
| `setuptools` | MIT package license plus separate licenses for its vendored build-tool components. It is a build/development dependency and is not included in the EventLens wheel. |

**Compatibility conclusion for this release boundary:** EventLens publishes its
own source and wheel with dependencies declared in package metadata; it does not
vendor dependency code. The built wheel has 162 entries and contains no
third-party dependency package roots. No license incompatibility was identified
for that separate-package boundary. This conclusion does not extend to a
container, installer, or other combined distribution that copies dependency
files alongside EventLens.

## Required notices

For the current source/wheel boundary, third-party dependencies are installed
from their own package distributions; their license and notice files remain
with those distributions. EventLens’s [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md)
states that boundary and links this inventory. No dependency license text needs
to be copied into the EventLens wheel because the wheel does not contain those
dependencies.

If a later release bundles dependencies, its release process must preserve each
bundled distribution’s license and notice files. In particular, it must retain
the complete platform-specific pypdfium2/PDFium license set, Pyphen’s GPL/LGPL/
MPL texts and dictionary attribution, the Vibe package NOTICE and Qlib
attribution, NumPy’s component licenses, and any Apache NOTICE files. A source
archive or application image containing dependencies needs a fresh, target-
platform inventory and notice bundle.

## Remaining limits

- This inventory is one macOS CPython 3.14.3 resolution, not a cross-platform
  lock. Markers and native wheel contents can change the notices for Linux,
  Windows, other Python versions, and later package releases.
- Dependency constraints in `pyproject.toml` are ranges, so future installs can
  select different versions. Regenerate and review the inventory for each
  release artifact.
- Provider data rights, market/news/transcript content, fonts or materials from
  other sources, and external service terms are outside this Python package
  license review.
- No qualified legal reviewer has provided a legal opinion. The engineering
  assessment is for EventLens’s non-vendored source/wheel distribution only.

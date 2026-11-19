from __future__ import annotations

from tests.product.test_research_evidence import _bundle

from eventlens.product.research_evidence import ResearchEvidenceBundle


class _BundleOnlySynthesisStub:
    """MVP-401 boundary probe: later synthesis receives facts, not providers."""

    def __init__(self) -> None:
        self.provider_calls = 0

    def synthesize(self, bundle: ResearchEvidenceBundle) -> dict[str, object]:
        assert isinstance(bundle, ResearchEvidenceBundle)
        # The stub receives only the immutable argument.  Any provider access
        # would have to be an explicit later-ticket dependency.
        return {
            "bundle_id": bundle.bundle_id,
            "bundle_sha256": bundle.bundle_sha256,
            "component_receipt_count": 1 + len(bundle.prior_cards),
        }


def test_serialized_bundle_is_the_only_synthesis_input_and_never_calls_provider(tmp_path) -> None:
    bundle = _bundle()
    path = tmp_path / "bundle.json"
    from eventlens.product.research_evidence import ResearchEvidenceStore

    ResearchEvidenceStore().write(bundle, path)
    reloaded = ResearchEvidenceStore().read(path)
    stub = _BundleOnlySynthesisStub()

    result = stub.synthesize(reloaded)

    assert result["bundle_id"] == "demo-evidence-1"
    assert result["bundle_sha256"] == reloaded.bundle_sha256
    assert stub.provider_calls == 0

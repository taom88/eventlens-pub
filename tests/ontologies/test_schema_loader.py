from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pydantic import ValidationError

from eventlens.artifacts.models import sha256_hex
from eventlens.ontologies import OntologyCatalog, OntologyError, load_layer
from eventlens.ontologies.schema import (
    OntologyParentPin, ResolvedOntology, SlotOverride, SlotValidator,
)


def test_packaged_sector_pack_resolves_deterministically() -> None:
    catalog = OntologyCatalog.packaged()
    first = catalog.resolve("sector.internet_platform")
    second = catalog.resolve("sector.internet_platform")

    assert first == second
    assert first.content_sha256 == second.content_sha256
    assert first.layer_ids == ("universal", "sector.internet_platform")
    assert {
        "platform.cloud_growth", "platform.cloud_margin",
        "platform.ai_capacity", "platform.ai_monetization",
    } <= set(first.critical_slot_ids)


def test_semiconductor_pack_reuses_only_general_layers() -> None:
    ontology = OntologyCatalog.packaged().resolve("sector.semiconductors")
    assert ontology.layer_ids == ("universal", "sector.semiconductors")
    assert "identity.ticker" in ontology.slots_by_id
    assert "semiconductor.supply_capacity" in ontology.slots_by_id
    assert not any(slot_id.startswith("demo.") for slot_id in ontology.slots_by_id)


@pytest.mark.parametrize(
    "body",
    [
        """schema_version: '1.0'\nontology_id: bad\nlayer_type: universal\nversion: 1.0.0\nslots:\n- {slot_id: bad.slot, definition: bad, aliases: [same, same], data_type: text, unit: text, cadence: static, source_priority: [issuer_filing], freshness: {max_age_days: 1, stale_behavior: warn}, materiality: low, extraction_mode: human, required_uses: [company_context], citation_policy: required, missing_value_policy: degrade}\n""",
        """schema_version: '1.0'\nontology_id: bad\nlayer_type: universal\nversion: 1.0.0\nslots:\n- {slot_id: bad.slot, definition: bad, aliases: [bad], data_type: text, unit: percent, cadence: static, source_priority: [issuer_filing], freshness: {max_age_days: 1, stale_behavior: warn}, materiality: low, extraction_mode: human, required_uses: [company_context], citation_policy: required, missing_value_policy: degrade}\n""",
        """schema_version: '1.0'\nontology_id: bad\nlayer_type: universal\nversion: 1.0.0\nslots:\n- {slot_id: bad.slot, definition: bad, aliases: [bad], data_type: decimal, unit: text, cadence: quarterly, source_priority: [issuer_filing], freshness: {max_age_days: 1, stale_behavior: warn}, materiality: low, extraction_mode: deterministic, required_uses: [company_context], citation_policy: required, missing_value_policy: degrade}\n""",
    ],
)
def test_bad_aliases_and_units_fail(tmp_path: Path, body: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(body)
    with pytest.raises(OntologyError):
        load_layer(path)


def test_unknown_inheritance_override_and_cycle_fail() -> None:
    universal = OntologyCatalog.packaged()._layers["universal"]
    sector = OntologyCatalog.packaged()._layers["sector.internet_platform"]
    unknown_parent = sector.model_copy(update={
        "ontology_id": "sector.bad", "extends": ("missing",),
        "parent_pins": (OntologyParentPin(
            ontology_id="missing", version="1.0.0", layer_sha256="a" * 64,
        ),),
    })
    with pytest.raises(OntologyError, match="unknown pinned"):
        OntologyCatalog((universal, unknown_parent)).resolve("sector.bad")

    unknown_override = sector.model_copy(
        update={"ontology_id": "sector.bad", "overrides": (SlotOverride(slot_id="missing.slot"),)}
    )
    with pytest.raises((OntologyError, AttributeError, TypeError, ValueError)):
        OntologyCatalog((universal, unknown_override)).resolve("sector.bad")

    # Content-addressed parent pins make a mutually recursive graph impossible
    # to author honestly; copied layers with unpinned inheritance fail at ingest.
    left = sector.model_copy(update={"ontology_id": "sector.left", "extends": ("sector.right",)})
    with pytest.raises(OntologyError, match="parent_pins"):
        OntologyCatalog((left,))


def test_component_version_changes_resolved_checksum() -> None:
    catalog = OntologyCatalog.packaged()
    baseline = catalog.resolve("sector.semiconductors")
    layers = list(catalog._layers.values())
    changed = [
        layer.model_copy(update={"version": "1.0.1"})
        if layer.ontology_id == "sector.semiconductors" else layer
        for layer in layers
    ]
    revised = OntologyCatalog(changed).resolve("sector.semiconductors")
    assert revised.content_sha256 != baseline.content_sha256


def test_universal_pack_covers_governance_revisions_language_and_news() -> None:
    slot_ids = OntologyCatalog.packaged().resolve("universal").slots_by_id
    assert {
        "governance.control_structure", "revisions.guidance_history",
        "management.language_change", "news.material_context",
    } <= set(slot_ids)


def test_validator_configuration_is_closed_and_typed() -> None:
    assert SlotValidator(validator_id="range", parameters={"min": 0, "max": 100})
    with pytest.raises(ValidationError):
        SlotValidator(validator_id="unknown")
    with pytest.raises(ValidationError):
        SlotValidator(validator_id="range", parameters={"min": 100, "max": 0})


def test_as_of_resolution_is_strict_and_selects_only_available_vintage() -> None:
    packaged = OntologyCatalog.packaged()
    base_layers = list(packaged._layers.values())
    semiconductor = packaged._layers["sector.semiconductors"]
    revised = semiconductor.model_copy(update={
        "version": "1.1.0",
        "released_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
        "effective_from": datetime(2026, 11, 1, tzinfo=timezone.utc),
    })
    catalog = OntologyCatalog((*base_layers, revised))

    october = catalog.resolve(
        "sector.semiconductors",
        as_of=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    december = catalog.resolve(
        "sector.semiconductors",
        as_of=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    assert october.version == "1.0.0"
    assert december.version == "1.1.0"
    assert catalog.resolve("sector.semiconductors", version="1.0.0") == october


def test_as_of_resolution_rejects_future_and_exact_release_boundary() -> None:
    catalog = OntologyCatalog.packaged()
    with pytest.raises(OntologyError, match="no cutoff-eligible"):
        catalog.resolve(
            "sector.internet_platform",
            as_of=datetime(2026, 10, 25, tzinfo=timezone.utc),
        )
    with pytest.raises(OntologyError, match="no cutoff-eligible"):
        catalog.resolve(
            "sector.internet_platform",
            as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )


def test_as_of_resolution_requires_utc() -> None:
    with pytest.raises(OntologyError, match="must use UTC"):
        OntologyCatalog.packaged().resolve(
            "sector.internet_platform",
            as_of=datetime(2026, 10, 26, tzinfo=timezone.utc).astimezone(
                timezone(timedelta(hours=-4))
            ),
        )


def test_explicit_child_pin_does_not_drift_when_new_parent_is_added() -> None:
    packaged = OntologyCatalog.packaged()
    old_parent = packaged._versions["universal"][0]
    child = packaged._layers["sector.semiconductors"]
    newer_parent = old_parent.model_copy(update={
        "version": "1.1.0",
        "released_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
        "effective_from": datetime(2026, 11, 1, tzinfo=timezone.utc),
    })
    catalog = OntologyCatalog((old_parent, newer_parent, child))

    resolved = catalog.resolve("sector.semiconductors", version="1.0.0")

    assert resolved.component_versions == (
        ("universal", "1.0.0"), ("sector.semiconductors", "1.0.0"),
    )


@pytest.mark.parametrize(
    ("released_at", "effective_from"),
    [
        (datetime(2026, 10, 26, tzinfo=timezone.utc), datetime(2026, 10, 27, tzinfo=timezone.utc)),
        (datetime(2026, 10, 27, tzinfo=timezone.utc), datetime(2026, 10, 26, tzinfo=timezone.utc)),
    ],
)
def test_each_ontology_timestamp_must_be_strictly_before_cutoff(
    released_at: datetime, effective_from: datetime,
) -> None:
    packaged = OntologyCatalog.packaged()
    layers = [
        layer for layer in packaged._layers.values()
        if layer.ontology_id != "sector.semiconductors"
    ]
    sector = packaged._layers["sector.semiconductors"]
    revised = sector.model_copy(update={
        "version": "1.1.0", "released_at": released_at, "effective_from": effective_from,
    })
    catalog = OntologyCatalog((*layers, revised))
    with pytest.raises(OntologyError, match="no cutoff-eligible"):
        catalog.resolve(
            "sector.semiconductors", as_of=datetime(2026, 10, 27, tzinfo=timezone.utc),
        )


def test_resolved_ontology_rejects_duplicate_or_mismatched_vintages() -> None:
    resolved = OntologyCatalog.packaged().resolve("sector.internet_platform")
    base = resolved.model_dump(mode="json")

    duplicated = dict(base)
    duplicated["component_vintages"] = [*base["component_vintages"], base["component_vintages"][-1]]
    duplicated["component_versions"] = [*base["component_versions"], base["component_versions"][-1]]
    duplicated["layer_ids"] = [*base["layer_ids"], base["layer_ids"][-1]]
    duplicated["content_sha256"] = sha256_hex({
        key: value for key, value in duplicated.items() if key != "content_sha256"
    })
    with pytest.raises(ValidationError, match="duplicate component vintages"):
        ResolvedOntology.model_validate(duplicated)

    mismatched = dict(base)
    mismatched["layer_ids"] = [*base["layer_ids"][:-1], "sector.other"]
    mismatched["content_sha256"] = sha256_hex({
        key: value for key, value in mismatched.items() if key != "content_sha256"
    })
    with pytest.raises(ValidationError, match="layer IDs"):
        ResolvedOntology.model_validate(mismatched)


def test_packaged_assets_are_immutable_version_addressed() -> None:
    catalog = OntologyCatalog.packaged()
    assert catalog.resolve("sector.internet_platform", version="1.0.0").version == "1.0.0"
    vintage_paths = tuple(
        Path("src/eventlens/ontologies/vintages").glob("*/*.yaml")
    )
    assert vintage_paths
    assert all(path.stem.count(".") == 2 for path in vintage_paths)

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eventlens.artifacts.models import sha256_hex
from eventlens.ontologies import OntologyCatalog, ResolvedOntology
from eventlens.ontologies.schema import SlotValidator
from eventlens.product.company_context import (
    CitedCompanyStatement,
    CompanyContextSource,
    CompanyContextArtifact,
    CompanyContextStore,
    CompanySlotObservation,
    ContextSourceType,
    OntologyUseMode,
    SlotState,
    build_company_context,
)


CUTOFF = datetime(2026, 10, 24, 20, tzinfo=timezone.utc)
SHA = "a" * 64


def _source(**updates):
    values = dict(
        source_id="demo-10k", source_type=ContextSourceType.ISSUER_FILING,
        source_sha256=SHA, available_at=CUTOFF - timedelta(days=30),
        citation="Example Systems 2025 Form 10-K", source_url="https://example.com/demo-10k",
        citation_locator="Item 1", event_id=None,
    )
    values.update(updates)
    return CompanyContextSource(**values)


def _build(**updates):
    source = updates.pop("source", _source())
    statement = CitedCompanyStatement(text="Example Systems operates cloud and commerce segments.", source_ids=(source.source_id,))
    values = dict(
        ticker="DEMO", artifact_version=1, built_at=CUTOFF,
        information_cutoff_at=CUTOFF,
        ontology=OntologyCatalog.packaged().resolve("sector.internet_platform"), sources=(source,),
        observations=(CompanySlotObservation(
            slot_id="identity.ticker", value="DEMO", unit="text",
            fact_period="current",
            as_of_at=source.available_at, source_ids=(source.source_id,),
        ),),
        identity=(statement,), management=(), segments=(statement,),
        revenue_drivers=(statement,), historical_overview=(statement,),
        target_event_id="DEMO:2026-10-29:2026Q3",
        ontology_use_mode=OntologyUseMode.CURRENT_METHOD_RETROSPECTIVE,
    )
    values.update(updates)
    return build_company_context(**values)


def test_company_context_is_deterministic_and_explicit_about_missing_critical_slots() -> None:
    first = _build()
    second = _build()
    assert first == second
    assert first.content_sha256 == second.content_sha256
    assert first.artifact_type == "company_context"
    assert not hasattr(first, "event_id")
    missing = {item.slot_id: item for item in first.slots if item.state is SlotState.MISSING}
    assert missing["platform.cloud_growth"].missing_reason == "no_cutoff_eligible_observation"
    assert first.critical_slots_populated == 1
    assert first.critical_slots_total > first.critical_slots_populated


def test_conflicting_sources_remain_visible() -> None:
    source = _source()
    observations = tuple(
        CompanySlotObservation(
            slot_id="identity.ticker", value=value, unit="text",
            fact_period="current",
            as_of_at=source.available_at, source_ids=(source.source_id,),
        )
        for value in ("DEMO", "OTHER")
    )
    artifact = _build(observations=observations)
    resolution = next(item for item in artifact.slots if item.slot_id == "identity.ticker")
    assert resolution.state is SlotState.CONFLICTED
    assert {item.value for item in resolution.observations} == {"DEMO", "OTHER"}


def test_version_change_changes_artifact_checksum() -> None:
    assert _build(artifact_version=1).content_sha256 != _build(artifact_version=2).content_sha256


def test_observation_type_and_unit_must_match_ontology() -> None:
    source = _source()
    for value, unit, message in (("not-a-number", "percent", "ontology type"), (12, "text", "unit")):
        observation = CompanySlotObservation(
            slot_id="financials.revenue_growth", value=value, unit=unit,
            fact_period="2026Q3",
            as_of_at=source.available_at, source_ids=(source.source_id,),
        )
        with pytest.raises(ValueError, match=message):
            _build(observations=(observation,))


@pytest.mark.parametrize(
    ("source", "observations", "message"),
    [
        (_source(available_at=CUTOFF), (), "strictly before cutoff"),
        (_source(event_id="DEMO:2026-10-29:2026Q3"), (), "target event outcome"),
    ],
)
def test_post_cutoff_and_target_event_sources_are_rejected(source, observations, message) -> None:
    with pytest.raises(ValueError, match=message):
        _build(source=source, observations=observations)


def test_company_context_round_trips_canonically(tmp_path) -> None:
    artifact = _build()
    target = tmp_path / "DEMO-company-context.json"
    digest = CompanyContextStore().write(artifact, target)
    assert len(digest) == 64
    assert CompanyContextStore().read(target) == artifact


def test_time_series_changes_are_not_conflicts_but_same_period_disagreement_is() -> None:
    source = _source()
    def observation(period, value):
        return CompanySlotObservation(
            slot_id="financials.revenue_growth", fact_period=period,
            value=value, unit="percent", as_of_at=source.available_at,
            source_ids=(source.source_id,),
        )
    series = _build(observations=(observation("2026Q1", 10), observation("2026Q2", 12)))
    resolution = next(item for item in series.slots if item.slot_id == "financials.revenue_growth")
    assert resolution.state is SlotState.POPULATED
    conflict = _build(observations=(observation("2026Q2", 10), observation("2026Q2", 12)))
    resolution = next(item for item in conflict.slots if item.slot_id == "financials.revenue_growth")
    assert resolution.state is SlotState.CONFLICTED


def test_observation_order_does_not_change_checksum() -> None:
    source = _source()
    values = tuple(CompanySlotObservation(
        slot_id="financials.revenue_growth", fact_period="2026Q2", value=value,
        unit="percent", as_of_at=source.available_at, source_ids=(source.source_id,),
    ) for value in (10, 12))
    assert _build(observations=values).content_sha256 == _build(observations=tuple(reversed(values))).content_sha256


def test_built_at_cannot_precede_cutoff() -> None:
    with pytest.raises(ValueError, match="built_at"):
        _build(built_at=CUTOFF - timedelta(seconds=1))


def test_event_scoped_source_requires_event_id() -> None:
    for source_type in (
        ContextSourceType.HISTORICAL_CONTEXT_CARD,
        ContextSourceType.TRANSCRIPT,
        ContextSourceType.EVENT_RESULT_FILING,
    ):
        with pytest.raises(ValueError, match="event-scoped"):
            _source(source_type=source_type)


def test_target_event_id_must_not_be_blank() -> None:
    with pytest.raises(ValueError, match="target_event_id"):
        _build(target_event_id=" ")


def test_exact_cutoff_observation_is_rejected() -> None:
    source = _source()
    observation = CompanySlotObservation(
        slot_id="identity.ticker", fact_period="current", value="DEMO", unit="text",
        as_of_at=CUTOFF, source_ids=(source.source_id,),
    )
    with pytest.raises(ValueError, match="strictly before cutoff"):
        _build(observations=(observation,))


def test_exclude_freshness_and_declared_validator_are_enforced() -> None:
    source = _source()
    stale = CompanySlotObservation(
        slot_id="expectations.guidance", fact_period="2026Q2", value="Old guidance",
        unit="text", as_of_at=CUTOFF - timedelta(days=121), source_ids=(source.source_id,),
    )
    artifact = _build(observations=(stale,))
    resolution = next(item for item in artifact.slots if item.slot_id == "expectations.guidance")
    assert resolution.state is SlotState.MISSING
    assert resolution.missing_reason == "all_observations_stale"
    assert resolution.excluded_stale_observations == (stale,)

    base = OntologyCatalog.packaged().resolve("sector.internet_platform")
    slots = tuple(
        slot.model_copy(update={"validators": (SlotValidator(validator_id="non_negative"),)})
        if slot.slot_id == "financials.revenue_growth" else slot
        for slot in base.slots
    )
    ontology = ResolvedOntology.create(
        ontology_id=base.ontology_id, version=base.version,
        component_versions=base.component_versions, layer_ids=base.layer_ids, slots=slots,
        component_vintages=base.component_vintages,
    )
    negative = CompanySlotObservation(
        slot_id="financials.revenue_growth", fact_period="2026Q3", value=-1,
        unit="percent", as_of_at=source.available_at, source_ids=(source.source_id,),
    )
    with pytest.raises(ValueError, match="non_negative"):
        _build(ontology=ontology, observations=(negative,))


def test_source_requires_readable_address_and_locator() -> None:
    with pytest.raises(ValueError, match="source_url"):
        _source(source_url="not-a-url")


def test_decimal_values_are_canonicalized_before_conflict_detection() -> None:
    source = _source()
    observations = tuple(CompanySlotObservation(
        slot_id="financials.revenue_growth", fact_period="2026Q3", value=value,
        unit="percent", as_of_at=source.available_at, source_ids=(source.source_id,),
    ) for value in (10, "10.0", Decimal("10.00")))
    artifact = _build(observations=observations)
    resolution = next(item for item in artifact.slots if item.slot_id == "financials.revenue_growth")
    assert resolution.state is SlotState.POPULATED
    assert {item.value for item in resolution.observations} == {Decimal("1E+1")}

    zeros = tuple(CompanySlotObservation(
        slot_id="financials.revenue_growth", fact_period="2026Q3", value=value,
        unit="percent", as_of_at=source.available_at, source_ids=(source.source_id,),
    ) for value in ("-0", Decimal("0.00")))
    zero_artifact = _build(observations=zeros)
    zero_resolution = next(item for item in zero_artifact.slots if item.slot_id == "financials.revenue_growth")
    assert zero_resolution.state is SlotState.POPULATED
    assert {item.value for item in zero_resolution.observations} == {Decimal(0)}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_financial_observations_are_rejected(value: str) -> None:
    source = _source()
    observation = CompanySlotObservation(
        slot_id="financials.revenue_growth", fact_period="2026Q3", value=value,
        unit="percent", as_of_at=source.available_at, source_ids=(source.source_id,),
    )
    with pytest.raises(ValueError, match="ontology type"):
        _build(observations=(observation,))


def test_vintage_mode_rejects_ontology_released_at_or_after_cutoff() -> None:
    with pytest.raises(ValueError, match="released and effective before cutoff"):
        _build(ontology_use_mode=OntologyUseMode.VINTAGE_BACKTEST)


def test_retrospective_mode_is_explicitly_persisted() -> None:
    artifact = _build(ontology_use_mode=OntologyUseMode.CURRENT_METHOD_RETROSPECTIVE)
    assert artifact.ontology_use_mode is OntologyUseMode.CURRENT_METHOD_RETROSPECTIVE
    assert artifact.ontology_component_vintages[-1].ontology_id == "sector.internet_platform"


def test_persisted_artifact_cannot_be_relabelled_as_vintage_backtest() -> None:
    payload = _build().model_dump(mode="json")
    payload["ontology_use_mode"] = OntologyUseMode.VINTAGE_BACKTEST.value
    payload["content_sha256"] = sha256_hex({
        key: value for key, value in payload.items() if key != "content_sha256"
    })

    with pytest.raises(ValueError, match="released and effective before cutoff"):
        CompanyContextArtifact.model_validate(payload)

"""Cutoff-safe, reusable company context derived from a resolved ontology."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.ontologies import OntologyComponentVintage, ResolvedOntology
from eventlens.ontologies.schema import SlotDataType


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ContextSourceType(StrEnum):
    ISSUER_FILING = "issuer_filing"
    EVENT_RESULT_FILING = "event_result_filing"
    ISSUER_RELEASE = "issuer_release"
    HISTORICAL_CONTEXT_CARD = "historical_context_card"
    TRANSCRIPT = "transcript"
    MARKET_DATA = "market_data"
    CONSENSUS = "consensus"
    NEWS = "news"
    ANALYST_INPUT = "analyst_input"


class SlotState(StrEnum):
    POPULATED = "populated"
    CONFLICTED = "conflicted"
    MISSING = "missing"


class OntologyUseMode(StrEnum):
    LIVE_CURRENT = "live_current"
    VINTAGE_BACKTEST = "vintage_backtest"
    CURRENT_METHOD_RETROSPECTIVE = "current_method_retrospective"


class CompanyContextSource(_FrozenModel):
    source_id: str = Field(min_length=1)
    source_type: ContextSourceType
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    available_at: datetime
    citation: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    citation_locator: str = Field(min_length=1)
    event_id: str | None = None

    @field_validator("available_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def provenance_is_addressable(self) -> "CompanyContextSource":
        parsed = urlparse(self.source_url)
        if parsed.scheme not in {"http", "https", "eventlens"} or not parsed.netloc:
            raise ValueError("source_url must be an absolute HTTP(S) or eventlens URL")
        event_scoped = {
            ContextSourceType.EVENT_RESULT_FILING,
            ContextSourceType.HISTORICAL_CONTEXT_CARD,
            ContextSourceType.ISSUER_RELEASE,
            ContextSourceType.TRANSCRIPT,
        }
        if self.source_type in event_scoped and not self.event_id:
            raise ValueError("event-scoped sources require event_id")
        return self


class CitedCompanyStatement(_FrozenModel):
    text: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)


class CompanySlotObservation(_FrozenModel):
    slot_id: str = Field(min_length=1)
    fact_period: str = Field(min_length=1)
    value: Any
    unit: str = Field(min_length=1)
    as_of_at: datetime
    source_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("as_of_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class CompanySlotResolution(_FrozenModel):
    slot_id: str
    state: SlotState
    observations: tuple[CompanySlotObservation, ...] = ()
    excluded_stale_observations: tuple[CompanySlotObservation, ...] = ()
    stale_warning_count: int = Field(default=0, ge=0)
    missing_reason: str | None = None

    @model_validator(mode="after")
    def shape_matches_state(self) -> "CompanySlotResolution":
        if self.state is SlotState.MISSING:
            if self.observations or not self.missing_reason:
                raise ValueError("missing slots require a reason and no observations")
        elif not self.observations or self.missing_reason is not None:
            raise ValueError("populated/conflicted slots require observations and no missing reason")
        return self


class CompanyContextArtifact(_FrozenModel):
    schema_version: str = "1.0"
    artifact_type: str = "company_context"
    artifact_id: str = Field(min_length=1)
    artifact_version: int = Field(ge=1)
    ticker: str = Field(min_length=1)
    built_at: datetime
    information_cutoff_at: datetime
    ontology_id: str
    ontology_version: str
    ontology_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ontology_use_mode: OntologyUseMode
    ontology_component_vintages: tuple[OntologyComponentVintage, ...] = Field(min_length=1)
    identity: tuple[CitedCompanyStatement, ...]
    management: tuple[CitedCompanyStatement, ...]
    segments: tuple[CitedCompanyStatement, ...]
    revenue_drivers: tuple[CitedCompanyStatement, ...]
    historical_overview: tuple[CitedCompanyStatement, ...]
    sources: tuple[CompanyContextSource, ...] = Field(min_length=1)
    slots: tuple[CompanySlotResolution, ...]
    critical_slots_populated: int = Field(ge=0)
    critical_slots_total: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("built_at", "information_cutoff_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def checksum_and_ontology_vintage_are_valid(self) -> "CompanyContextArtifact":
        if sha256_hex(self.model_dump(mode="json", exclude={"content_sha256"})) != self.content_sha256:
            raise ValueError("company-context checksum mismatch")
        terminal = self.ontology_component_vintages[-1]
        if (terminal.ontology_id, terminal.version) != (
            self.ontology_id, self.ontology_version,
        ):
            raise ValueError("company-context ontology does not match terminal vintage")
        if self.ontology_use_mode in {
            OntologyUseMode.LIVE_CURRENT,
            OntologyUseMode.VINTAGE_BACKTEST,
        } and any(
            component.released_at >= self.information_cutoff_at
            or component.effective_from >= self.information_cutoff_at
            for component in self.ontology_component_vintages
        ):
            raise ValueError(
                "live/vintage company context requires ontology components "
                "released and effective before cutoff"
            )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


def build_company_context(
    *, ticker: str, artifact_version: int, built_at: datetime,
    information_cutoff_at: datetime, ontology: ResolvedOntology,
    sources: tuple[CompanyContextSource, ...],
    observations: tuple[CompanySlotObservation, ...],
    identity: tuple[CitedCompanyStatement, ...],
    management: tuple[CitedCompanyStatement, ...],
    segments: tuple[CitedCompanyStatement, ...],
    revenue_drivers: tuple[CitedCompanyStatement, ...],
    historical_overview: tuple[CitedCompanyStatement, ...],
    target_event_id: str,
    ontology_use_mode: OntologyUseMode,
) -> CompanyContextArtifact:
    if not target_event_id.strip():
        raise ValueError("target_event_id must not be blank")
    cutoff = _utc(information_cutoff_at)
    built = _utc(built_at)
    if built < cutoff:
        raise ValueError("built_at cannot precede information_cutoff_at")
    if ontology_use_mode in {
        OntologyUseMode.LIVE_CURRENT,
        OntologyUseMode.VINTAGE_BACKTEST,
    } and any(
        component.released_at >= cutoff or component.effective_from >= cutoff
        for component in ontology.component_vintages
    ):
        raise ValueError(
            "live/vintage company context requires ontology components released and effective before cutoff"
        )
    source_by_id = {item.source_id: item for item in sources}
    if len(source_by_id) != len(sources):
        raise ValueError("company-context sources must have unique IDs")
    if any(item.available_at >= cutoff for item in sources):
        raise ValueError("company context sources must be strictly before cutoff")
    if any(item.event_id == target_event_id for item in sources):
        raise ValueError("company context cannot include the target event outcome")
    statements = (*identity, *management, *segments, *revenue_drivers, *historical_overview)
    referenced = {source_id for statement in statements for source_id in statement.source_ids}
    for raw_observation in observations:
        observation = raw_observation
        if observation.as_of_at >= cutoff:
            raise ValueError("company context observations must be strictly before cutoff")
        referenced.update(observation.source_ids)
    if unknown := referenced - source_by_id.keys():
        raise ValueError(f"company context references unknown sources: {sorted(unknown)!r}")

    grouped: dict[str, list[CompanySlotObservation]] = {}
    for observation in observations:
        slot = ontology.slots_by_id.get(observation.slot_id)
        if slot is None:
            raise ValueError(f"observation references unknown ontology slot {observation.slot_id!r}")
        if observation.unit != slot.unit.value:
            raise ValueError(f"observation unit does not match ontology slot {observation.slot_id!r}")
        _validate_observation_value(observation.value, slot.data_type, observation.slot_id)
        if slot.data_type is SlotDataType.DECIMAL:
            parsed_decimal = Decimal(str(observation.value))
            canonical_decimal = Decimal(0) if parsed_decimal.is_zero() else parsed_decimal.normalize()
            observation = observation.model_copy(update={"value": canonical_decimal})
        grouped.setdefault(observation.slot_id, []).append(observation)

    resolutions: list[CompanySlotResolution] = []
    for slot in ontology.slots:
        all_values = tuple(sorted(
            grouped.get(slot.slot_id, ()),
            key=lambda item: (item.fact_period, item.as_of_at, sha256_hex(item.model_dump(mode="json"))),
        ))
        excluded_stale: list[CompanySlotObservation] = []
        stale_warning_count = 0
        values_list: list[CompanySlotObservation] = []
        for item in all_values:
            _apply_slot_validators(item.value, slot.validators, slot.slot_id)
            max_age = slot.freshness.max_age_days
            stale = max_age is not None and cutoff - item.as_of_at > timedelta(days=max_age)
            if stale and slot.freshness.stale_behavior == "exclude":
                excluded_stale.append(item)
            else:
                values_list.append(item)
                if stale and slot.freshness.stale_behavior == "warn":
                    stale_warning_count += 1
        values = tuple(values_list)
        if not values:
            state = SlotState.MISSING
            reason = (
                "all_observations_stale" if excluded_stale
                else "no_cutoff_eligible_observation"
            )
        else:
            period_values: dict[str, set[str]] = {}
            for item in values:
                period_values.setdefault(item.fact_period, set()).add(sha256_hex(item.value))
            state = (
                SlotState.CONFLICTED
                if any(len(items) > 1 for items in period_values.values())
                else SlotState.POPULATED
            )
            reason = None
        resolutions.append(CompanySlotResolution(
            slot_id=slot.slot_id, state=state, observations=values,
            excluded_stale_observations=tuple(excluded_stale),
            stale_warning_count=stale_warning_count, missing_reason=reason,
        ))
    critical = set(ontology.critical_slot_ids)
    critical_populated = sum(
        item.slot_id in critical and item.state is SlotState.POPULATED for item in resolutions
    )
    payload = dict(
        schema_version="1.0", artifact_type="company_context",
        artifact_id=f"{ticker.strip().lower()}-company-context-v{artifact_version}",
        artifact_version=artifact_version, ticker=ticker, built_at=built,
        information_cutoff_at=cutoff, ontology_id=ontology.ontology_id,
        ontology_version=ontology.version, ontology_sha256=ontology.content_sha256,
        ontology_use_mode=ontology_use_mode,
        ontology_component_vintages=ontology.component_vintages,
        identity=identity, management=management, segments=segments,
        revenue_drivers=revenue_drivers, historical_overview=historical_overview,
        sources=tuple(sorted(sources, key=lambda item: item.source_id)),
        slots=tuple(resolutions), critical_slots_populated=critical_populated,
        critical_slots_total=len(critical),
    )
    provisional = CompanyContextArtifact.model_construct(**payload, content_sha256="0" * 64)
    json_payload = provisional.model_dump(mode="json", exclude={"content_sha256"})
    return CompanyContextArtifact.model_validate(payload | {"content_sha256": sha256_hex(json_payload)})


class CompanyContextStore:
    def write(self, artifact: CompanyContextArtifact, target: Path) -> str:
        payload = artifact.canonical_bytes() + b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return digest

    def read(self, target: Path) -> CompanyContextArtifact:
        artifact = CompanyContextArtifact.model_validate_json(target.read_bytes())
        if target.read_bytes() != artifact.canonical_bytes() + b"\n":
            raise ValueError("company-context bytes are not canonical")
        return artifact


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_observation_value(value: Any, data_type: SlotDataType, slot_id: str) -> None:
    valid = False
    if data_type is SlotDataType.DECIMAL and not isinstance(value, bool):
        try:
            valid = Decimal(str(value)).is_finite()
        except (InvalidOperation, ValueError):
            pass
    elif data_type is SlotDataType.INTEGER:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif data_type in {SlotDataType.TEXT, SlotDataType.ENUM}:
        valid = isinstance(value, str) and bool(value.strip())
    elif data_type is SlotDataType.BOOLEAN:
        valid = isinstance(value, bool)
    elif data_type is SlotDataType.DATE:
        valid = isinstance(value, date) and not isinstance(value, datetime)
    elif data_type is SlotDataType.DATETIME:
        valid = isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    if not valid:
        raise ValueError(f"observation value does not match ontology type for {slot_id!r}")


def _apply_slot_validators(value: Any, validators: tuple[Any, ...], slot_id: str) -> None:
    for validator in validators:
        if validator.validator_id == "non_negative":
            if Decimal(str(value)) < 0:
                raise ValueError(f"slot validator non_negative failed for {slot_id!r}")
        elif validator.validator_id == "range":
            numeric = Decimal(str(value))
            minimum = validator.parameters.get("min")
            maximum = validator.parameters.get("max")
            if minimum is not None and numeric < Decimal(str(minimum)):
                raise ValueError(f"slot validator range failed for {slot_id!r}")
            if maximum is not None and numeric > Decimal(str(maximum)):
                raise ValueError(f"slot validator range failed for {slot_id!r}")
        elif validator.validator_id == "one_of":
            allowed = validator.parameters.get("values")
            if not isinstance(allowed, list) or value not in allowed:
                raise ValueError(f"slot validator one_of failed for {slot_id!r}")
        else:
            raise ValueError(f"unknown slot validator {validator.validator_id!r} for {slot_id!r}")


__all__ = [
    "CitedCompanyStatement", "CompanyContextArtifact", "CompanyContextSource",
    "CompanyContextStore", "CompanySlotObservation", "CompanySlotResolution",
    "ContextSourceType", "OntologyUseMode", "SlotState", "build_company_context",
]

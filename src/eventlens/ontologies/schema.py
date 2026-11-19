"""Typed, layered company-context ontology contracts."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.artifacts.models import sha256_hex


ID_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"
VERSION_PATTERN = r"^\d+\.\d+\.\d+$"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class OntologyLayerType(StrEnum):
    UNIVERSAL = "universal"
    SECTOR = "sector"
    ISSUER = "issuer"


class SlotDataType(StrEnum):
    DECIMAL = "decimal"
    INTEGER = "integer"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    ENUM = "enum"


class SlotUnit(StrEnum):
    NONE = "none"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    COUNT = "count"
    RATIO = "ratio"
    PERCENT = "percent"
    BASIS_POINTS = "basis_points"
    CURRENCY = "currency"
    CURRENCY_MILLIONS = "currency_millions"
    CURRENCY_BILLIONS = "currency_billions"
    SHARES = "shares"
    DAYS = "days"
    MULTIPLE = "multiple"
    CAPACITY = "capacity"


class SlotCadence(StrEnum):
    STATIC = "static"
    EVENT = "event"
    DAILY = "daily"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class SourceKind(StrEnum):
    ISSUER_FILING = "issuer_filing"
    ISSUER_RELEASE = "issuer_release"
    TRANSCRIPT = "transcript"
    MARKET_DATA = "market_data"
    CONSENSUS = "consensus"
    NEWS = "news"
    ANALYST_INPUT = "analyst_input"


class SlotMateriality(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExtractionMode(StrEnum):
    DETERMINISTIC = "deterministic"
    STRUCTURED_PROVIDER = "structured_provider"
    LLM_STRUCTURED = "llm_structured"
    HUMAN = "human"


class CitationPolicy(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class MissingValuePolicy(StrEnum):
    BLOCK_RELEASE = "block_release"
    EXPLICIT_NULL = "explicit_null"
    DEGRADE = "degrade"


class RequiredUse(StrEnum):
    COMPANY_CONTEXT = "company_context"
    EVIDENCE_RETRIEVAL = "evidence_retrieval"
    PRE_EARNINGS_THESIS = "pre_earnings_thesis"
    CONSENSUS = "consensus"
    VALUATION = "valuation"
    RISK = "risk"
    NEWS = "news"


class FreshnessPolicy(_FrozenModel):
    max_age_days: int | None = Field(default=None, ge=0)
    stale_behavior: str = Field(pattern=r"^(warn|exclude|allow_static)$")


class SlotValidator(_FrozenModel):
    validator_id: str = Field(pattern=ID_PATTERN)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def supported_configuration(self) -> "SlotValidator":
        if self.validator_id == "non_negative":
            if self.parameters:
                raise ValueError("non_negative validator takes no parameters")
        elif self.validator_id == "range":
            if not self.parameters or set(self.parameters) - {"min", "max"}:
                raise ValueError("range validator requires only min and/or max")
            try:
                minimum = self.parameters.get("min")
                maximum = self.parameters.get("max")
                minimum_decimal = Decimal(str(minimum)) if minimum is not None else None
                maximum_decimal = Decimal(str(maximum)) if maximum is not None else None
                if minimum_decimal is not None and not minimum_decimal.is_finite():
                    raise ValueError("range validator bounds must be finite")
                if maximum_decimal is not None and not maximum_decimal.is_finite():
                    raise ValueError("range validator bounds must be finite")
                if minimum_decimal is not None and maximum_decimal is not None and minimum_decimal > maximum_decimal:
                    raise ValueError("range validator min cannot exceed max")
            except (InvalidOperation, TypeError, ValueError) as error:
                raise ValueError("range validator bounds must be numeric") from error
        elif self.validator_id == "one_of":
            values = self.parameters.get("values")
            if set(self.parameters) != {"values"} or not isinstance(values, list) or not values:
                raise ValueError("one_of validator requires a non-empty values list")
        else:
            raise ValueError(f"unsupported slot validator {self.validator_id!r}")
        return self


class OntologySlot(_FrozenModel):
    slot_id: str = Field(pattern=ID_PATTERN)
    definition: str = Field(min_length=1)
    aliases: tuple[str, ...] = Field(min_length=1)
    data_type: SlotDataType
    unit: SlotUnit
    cadence: SlotCadence
    source_priority: tuple[SourceKind, ...] = Field(min_length=1)
    freshness: FreshnessPolicy
    materiality: SlotMateriality
    extraction_mode: ExtractionMode
    validators: tuple[SlotValidator, ...] = ()
    required_uses: tuple[RequiredUse, ...] = Field(min_length=1)
    citation_policy: CitationPolicy
    missing_value_policy: MissingValuePolicy

    @field_validator("aliases")
    @classmethod
    def aliases_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [_normalize_alias(item) for item in values]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("slot aliases must be non-empty and unique after normalization")
        return values

    @model_validator(mode="after")
    def unit_matches_data_type(self) -> "OntologySlot":
        compatible = {
            SlotDataType.TEXT: {SlotUnit.TEXT},
            SlotDataType.ENUM: {SlotUnit.TEXT},
            SlotDataType.BOOLEAN: {SlotUnit.BOOLEAN},
            SlotDataType.DATE: {SlotUnit.DATE},
            SlotDataType.DATETIME: {SlotUnit.DATETIME},
            SlotDataType.INTEGER: {SlotUnit.COUNT, SlotUnit.SHARES, SlotUnit.DAYS, SlotUnit.CAPACITY},
            SlotDataType.DECIMAL: {
                SlotUnit.COUNT, SlotUnit.RATIO, SlotUnit.PERCENT, SlotUnit.BASIS_POINTS,
                SlotUnit.CURRENCY, SlotUnit.CURRENCY_MILLIONS, SlotUnit.CURRENCY_BILLIONS,
                SlotUnit.SHARES, SlotUnit.DAYS, SlotUnit.MULTIPLE, SlotUnit.CAPACITY,
            },
        }
        if self.unit not in compatible[self.data_type]:
            raise ValueError(f"unit={self.unit.value} is incompatible with data_type={self.data_type.value}")
        return self


class SlotOverride(_FrozenModel):
    slot_id: str = Field(pattern=ID_PATTERN)
    aliases: tuple[str, ...] | None = Field(default=None, min_length=1)
    source_priority: tuple[SourceKind, ...] | None = Field(default=None, min_length=1)
    freshness: FreshnessPolicy | None = None
    materiality: SlotMateriality | None = None
    extraction_mode: ExtractionMode | None = None
    validators: tuple[SlotValidator, ...] | None = None
    required_uses: tuple[RequiredUse, ...] | None = Field(default=None, min_length=1)
    citation_policy: CitationPolicy | None = None
    missing_value_policy: MissingValuePolicy | None = None

    @field_validator("aliases")
    @classmethod
    def aliases_are_unique(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is None:
            return None
        normalized = [_normalize_alias(item) for item in values]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("override aliases must be non-empty and unique after normalization")
        return values


class OntologyParentPin(_FrozenModel):
    ontology_id: str = Field(pattern=ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    layer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class OntologyLayer(_FrozenModel):
    schema_version: str = "1.0"
    ontology_id: str = Field(pattern=ID_PATTERN)
    layer_type: OntologyLayerType
    version: str = Field(pattern=VERSION_PATTERN)
    released_at: datetime
    effective_from: datetime
    extends: tuple[str, ...] = ()
    parent_pins: tuple[OntologyParentPin, ...] = ()
    slots: tuple[OntologySlot, ...] = ()
    overrides: tuple[SlotOverride, ...] = ()

    @field_validator("released_at", "effective_from")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ontology timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def layer_is_consistent(self) -> "OntologyLayer":
        if self.layer_type is OntologyLayerType.UNIVERSAL and self.extends:
            raise ValueError("universal ontology cannot extend another layer")
        parent_ids = tuple(item.ontology_id for item in self.parent_pins)
        if parent_ids != tuple(sorted(self.extends)):
            raise ValueError("parent_pins must exactly match sorted extends IDs")
        if len(parent_ids) != len(set(parent_ids)):
            raise ValueError("ontology layer cannot repeat parent pins")
        ids = [item.slot_id for item in self.slots]
        if len(ids) != len(set(ids)):
            raise ValueError("ontology layer contains duplicate slot IDs")
        override_ids = [item.slot_id for item in self.overrides]
        if len(override_ids) != len(set(override_ids)):
            raise ValueError("ontology layer contains duplicate overrides")
        if set(ids) & set(override_ids):
            raise ValueError("a layer cannot define and override the same slot")
        return self


class OntologyComponentVintage(_FrozenModel):
    ontology_id: str = Field(pattern=ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    released_at: datetime
    effective_from: datetime
    layer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("released_at", "effective_from")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ontology vintage timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class ResolvedOntology(_FrozenModel):
    schema_version: str = "1.0"
    ontology_id: str = Field(pattern=ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    component_versions: tuple[tuple[str, str], ...] = Field(min_length=1)
    component_vintages: tuple[OntologyComponentVintage, ...] = Field(min_length=1)
    layer_ids: tuple[str, ...] = Field(min_length=1)
    slots: tuple[OntologySlot, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def create(
        cls,
        *,
        ontology_id: str,
        version: str,
        component_versions: tuple[tuple[str, str], ...],
        component_vintages: tuple[OntologyComponentVintage, ...],
        layer_ids: tuple[str, ...],
        slots: tuple[OntologySlot, ...],
    ) -> "ResolvedOntology":
        payload = {
            "schema_version": "1.0",
            "ontology_id": ontology_id,
            "version": version,
            "component_versions": component_versions,
            "component_vintages": [item.model_dump(mode="json") for item in component_vintages],
            "layer_ids": layer_ids,
            "slots": [item.model_dump(mode="json") for item in slots],
        }
        return cls(**payload, content_sha256=sha256_hex(payload))

    @model_validator(mode="after")
    def checksum_and_aliases_are_valid(self) -> "ResolvedOntology":
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if sha256_hex(payload) != self.content_sha256:
            raise ValueError("resolved ontology checksum mismatch")
        slot_ids = [slot.slot_id for slot in self.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("resolved ontology contains duplicate slot IDs")
        expected_versions = tuple(
            (item.ontology_id, item.version) for item in self.component_vintages
        )
        if self.component_versions != expected_versions:
            raise ValueError("component versions and timestamped vintages disagree")
        vintage_ids = tuple(item.ontology_id for item in self.component_vintages)
        if len(vintage_ids) != len(set(vintage_ids)):
            raise ValueError("resolved ontology contains duplicate component vintages")
        if self.layer_ids != vintage_ids:
            raise ValueError("layer IDs and timestamped vintages disagree")
        terminal = self.component_vintages[-1]
        if (terminal.ontology_id, terminal.version) != (self.ontology_id, self.version):
            raise ValueError("resolved ontology target does not match terminal component vintage")
        owners: dict[str, str] = {}
        for slot in self.slots:
            for alias in (slot.slot_id, *slot.aliases):
                normalized = _normalize_alias(alias)
                prior = owners.setdefault(normalized, slot.slot_id)
                if prior != slot.slot_id:
                    raise ValueError(
                        f"normalized alias {normalized!r} is shared by {prior!r} and {slot.slot_id!r}"
                    )
        return self

    @property
    def slots_by_id(self) -> dict[str, OntologySlot]:
        return {item.slot_id: item for item in self.slots}

    @property
    def critical_slot_ids(self) -> tuple[str, ...]:
        return tuple(
            item.slot_id for item in self.slots
            if item.materiality is SlotMateriality.CRITICAL
        )


def _normalize_alias(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold().replace("&", " and ")))


__all__ = [
    "CitationPolicy", "ExtractionMode", "FreshnessPolicy", "MissingValuePolicy",
    "OntologyLayer", "OntologyLayerType", "OntologyParentPin", "OntologySlot", "RequiredUse",
    "OntologyComponentVintage", "ResolvedOntology", "SlotCadence", "SlotDataType", "SlotMateriality",
    "SlotOverride", "SlotUnit", "SlotValidator", "SourceKind",
]

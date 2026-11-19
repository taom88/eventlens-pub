"""Versioned company KPI ontologies for EventLens context cards.

The ontology is metadata, not extracted issuer data.  It names KPI slots and
their semantic constraints so later extraction and retrieval tickets do not
have to infer units, periods, accounting bases, or recency behavior.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from importlib import resources
import re
from typing import Annotated, Any, Literal, TYPE_CHECKING

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
import yaml

from .metrics import MetricUnit

if TYPE_CHECKING:
    from .models import CompanyOntology


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CanonicalId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"),
]
Version = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$",
    ),
]


class _OntologyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class MetricTier(StrEnum):
    """Materiality tier used for KPI coverage and review."""

    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3 = "tier_3"
    TIER_4 = "tier_4"


class Directionality(StrEnum):
    """Default thesis interpretation, never a trading recommendation."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    CONDITIONAL = "conditional"
    NEUTRAL = "neutral"


class PeriodFamily(StrEnum):
    """Period identity required for a KPI observation."""

    FISCAL_QUARTER = "fiscal_quarter"
    NEXT_FISCAL_QUARTER = "next_fiscal_quarter"
    FISCAL_QUARTER_END = "fiscal_quarter_end"


class RecencyRule(_OntologyModel):
    """A versioned rule for choosing the current observation in a family."""

    rule_id: CanonicalId
    version: Version
    description: NonEmptyStr


class MetricFamily(_OntologyModel):
    """A versioned group of KPIs sharing period and recency semantics."""

    family_id: CanonicalId
    version: Version
    period_family: PeriodFamily
    recency_rule: CanonicalId


class KpiDefinition(_OntologyModel):
    """Canonical, issuer-specific definition of one KPI slot."""

    kpi_id: CanonicalId
    approval_status: Literal["proposed_pending_review", "approved"] = "proposed_pending_review"
    label: NonEmptyStr
    tier: MetricTier
    aliases: tuple[NonEmptyStr, ...] = Field(min_length=1)
    unit: MetricUnit
    period_family: PeriodFamily
    metric_family: CanonicalId
    recency_rule: CanonicalId
    normalization_rules: tuple[NonEmptyStr, ...] = Field(min_length=1)
    directionality: Directionality
    contextual_semantics: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def conditional_metrics_explain_their_context(self) -> "KpiDefinition":
        if self.directionality is Directionality.CONDITIONAL and not self.contextual_semantics:
            raise ValueError("conditional directionality requires contextual_semantics")
        return self


def _normalize_alias(value: str) -> str:
    """Normalize an alias for deterministic, punctuation-insensitive matching."""
    if not isinstance(value, str):
        raise TypeError("KPI alias must be a string")
    normalized = value.strip().casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


class KpiOntology(_OntologyModel):
    """Immutable identity and KPI registry loaded from a packaged resource."""

    ontology_id: CanonicalId
    ticker: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9.-]*$")]
    company: NonEmptyStr
    version: Version
    reference: NonEmptyStr
    review_status: Literal["proposed_pending_analyst_review", "tier_1_approved"]
    review_notes: tuple[NonEmptyStr, ...] = Field(min_length=1)
    recency_rules: tuple[RecencyRule, ...] = Field(min_length=1)
    metric_families: tuple[MetricFamily, ...] = Field(min_length=1)
    kpis: tuple[KpiDefinition, ...] = Field(min_length=1)

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def registry_is_internally_consistent(self) -> "KpiOntology":
        recency_by_id = _unique_by_id(self.recency_rules, "rule_id", "recency rule")
        family_by_id = _unique_by_id(self.metric_families, "family_id", "metric family")
        kpi_by_id = _unique_by_id(self.kpis, "kpi_id", "KPI")

        for family in self.metric_families:
            if family.recency_rule not in recency_by_id:
                raise ValueError(
                    f"metric family {family.family_id!r} references unknown recency rule "
                    f"{family.recency_rule!r}"
                )

        alias_owners: dict[str, str] = {}
        accepted_spelling_owners: dict[str, str] = {}
        for kpi in self.kpis:
            if kpi.approval_status == "approved" and kpi.tier is not MetricTier.TIER_1:
                raise ValueError("only Tier-1 KPI definitions may be approved in this ontology revision")
            family = family_by_id.get(kpi.metric_family)
            if family is None:
                raise ValueError(
                    f"KPI {kpi.kpi_id!r} references unknown metric family {kpi.metric_family!r}"
                )
            if kpi.recency_rule != family.recency_rule:
                raise ValueError(
                    f"KPI {kpi.kpi_id!r} recency rule does not match its metric family"
                )
            if kpi.period_family is not family.period_family:
                raise ValueError(
                    f"KPI {kpi.kpi_id!r} period family does not match its metric family"
                )

            for alias in kpi.aliases:
                normalized_alias = _normalize_alias(alias)
                prior_alias_owner = alias_owners.get(normalized_alias)
                if prior_alias_owner is not None:
                    raise ValueError(
                        f"normalized alias {normalized_alias!r} is duplicated by KPIs "
                        f"{prior_alias_owner!r} and {kpi.kpi_id!r}"
                    )
                alias_owners[normalized_alias] = kpi.kpi_id

            # Canonical IDs and labels participate in the same resolution
            # namespace, so accepted spellings cannot point to different KPIs.
            for candidate in (kpi.kpi_id, kpi.label, *kpi.aliases):
                normalized = _normalize_alias(candidate)
                if not normalized:
                    raise ValueError(f"KPI {kpi.kpi_id!r} contains an empty normalized alias")
                owner = accepted_spelling_owners.setdefault(normalized, kpi.kpi_id)
                if owner != kpi.kpi_id:
                    raise ValueError(
                        f"normalized alias {normalized!r} is shared by KPIs {owner!r} "
                        f"and {kpi.kpi_id!r}"
                    )

        if len(kpi_by_id) != len(self.kpis):  # pragma: no cover - guarded above
            raise ValueError("duplicate KPI IDs")
        return self

    @property
    def kpis_by_id(self) -> dict[str, KpiDefinition]:
        """Return a fresh lookup mapping without exposing mutable model state."""
        return {definition.kpi_id: definition for definition in self.kpis}

    def definitions_for_tier(self, tier: MetricTier) -> tuple[KpiDefinition, ...]:
        return tuple(definition for definition in self.kpis if definition.tier is tier)

    def resolve_alias(self, alias: str) -> str | None:
        """Resolve a canonical ID, label, or alias to its canonical KPI ID."""
        normalized = _normalize_alias(alias)
        for definition in self.kpis:
            candidates = (definition.kpi_id, definition.label, *definition.aliases)
            if any(_normalize_alias(candidate) == normalized for candidate in candidates):
                return definition.kpi_id
        return None

    def company_ontology_reference(self) -> "CompanyOntology":
        """Create the compact v4-card reference, without copying the registry."""
        from .models import CompanyOntology

        return CompanyOntology(
            ontology_id=self.ontology_id,
            version=self.version,
            reference=self.reference,
        )


def _unique_by_id(items: tuple[Any, ...], attribute: str, kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        item_id = getattr(item, attribute)
        if item_id in result:
            raise ValueError(f"duplicate {kind} ID: {item_id!r}")
        result[item_id] = item
    return result


@lru_cache(maxsize=None)
def load_ontology(ticker: str) -> KpiOntology:
    """Load and validate a packaged issuer ontology by ticker.

    Unsupported tickers fail closed.  This ticket intentionally ships no
    generic fallback and no ontology for another issuer.
    """
    if not isinstance(ticker, str):
        raise TypeError("ticker must be a string")
    normalized_ticker = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]*", normalized_ticker):
        raise ValueError(f"invalid ticker: {ticker!r}")

    resource = resources.files("eventlens.context_cards.ontologies").joinpath(
        f"{normalized_ticker}.yaml"
    )
    if not resource.is_file():
        raise LookupError(f"no packaged KPI ontology for ticker {normalized_ticker!r}")

    payload = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"ontology resource {resource.name!r} must contain a YAML mapping")
    ontology = KpiOntology.model_validate(payload)
    if ontology.ticker != normalized_ticker:
        raise ValueError(
            f"ontology resource ticker {ontology.ticker!r} does not match {normalized_ticker!r}"
        )
    return ontology


def resolve_alias(
    alias: str,
    ontology: KpiOntology | str,
) -> str | None:
    """Resolve an alias using an explicitly selected issuer ontology."""
    if isinstance(ontology, str):
        registry = load_ontology(ontology)
    elif isinstance(ontology, KpiOntology):
        registry = ontology
    else:
        raise TypeError("ontology must be a KpiOntology or ticker string")
    return registry.resolve_alias(alias)


__all__ = [
    "Directionality",
    "KpiDefinition",
    "KpiOntology",
    "MetricTier",
    "load_ontology",
    "resolve_alias",
]

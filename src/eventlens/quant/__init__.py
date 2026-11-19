"""Event-conditioned quant validation and scenario-only analysis."""

from .event_study import (
    alpha_zoo_assessment,
    apply_transaction_costs,
    beta_regression,
    bootstrap_mean_confidence_interval,
    market_assumptions,
    validate_event_cohort,
)
from .models import *  # noqa: F403
from .optimization import optimize_worst_case_scenario
from .backtest import (
    EventStudyObservation,
    EventStudyReport,
    canonical_event_study_bytes,
    event_study_report_sha256,
    run_event_study,
)
from .pre_event_features import PreEventFeatureSnapshot, build_pre_event_features, compact_provenance_manifest
from .inputs import (
    AnalyticsEligibility,
    AnalyticsSourceReceipt,
    AnalyticsSourceRole,
    EventAnalyticsInputBundle,
    EventAnalyticsInputManifest,
    assemble_event_analytics_inputs,
    run_event_study_from_bundle,
)
from .supplement import (
    BenchmarkEventObservation,
    BenchmarkRelativeEventMetrics,
    CostScenarioPolicy,
    EventStudySupplement,
    build_event_study_supplement,
)

__all__ = [
    "alpha_zoo_assessment", "apply_transaction_costs", "beta_regression",
    "bootstrap_mean_confidence_interval", "market_assumptions", "validate_event_cohort",
    "optimize_worst_case_scenario",
    "EventStudyObservation", "EventStudyReport", "canonical_event_study_bytes",
    "event_study_report_sha256", "run_event_study",
    "PreEventFeatureSnapshot", "build_pre_event_features", "compact_provenance_manifest",
    "AnalyticsEligibility", "AnalyticsSourceReceipt", "AnalyticsSourceRole",
    "EventAnalyticsInputBundle", "EventAnalyticsInputManifest",
    "assemble_event_analytics_inputs", "run_event_study_from_bundle",
    "BenchmarkEventObservation", "BenchmarkRelativeEventMetrics", "CostScenarioPolicy",
    "EventStudySupplement", "build_event_study_supplement",
]

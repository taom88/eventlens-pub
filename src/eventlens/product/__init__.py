"""User-facing intent-to-run orchestration."""

from .ask import (
    ResearchAskResult,
    render_research_ask_markdown,
    run_research_ask,
    write_research_markdown,
)

__all__ = [
    "ResearchAskResult", "render_research_ask_markdown",
    "run_research_ask", "write_research_markdown",
]
from .fixture_replay import (
    DEFAULT_FIXTURE_ROOT,
    FIXTURE_SCHEMA_VERSION,
    FixtureBaseline,
    FixtureContextCard,
    FixtureMissingValue,
    FixturePreEventFeatures,
    FixtureReactionMetrics,
    FixtureManifest,
    FixtureReplayError,
    FixtureReplayPackage,
    FixtureReplayPreparation,
    FixtureReplayRun,
    load_d0_demo_fixture,
    prepare_d0_demo_fixture_replay,
    run_d0_demo_fixture_replay,
)

__all__ = [
    "DEFAULT_FIXTURE_ROOT",
    "FIXTURE_SCHEMA_VERSION",
    "FixtureBaseline",
    "FixtureContextCard",
    "FixtureMissingValue",
    "FixturePreEventFeatures",
    "FixtureReactionMetrics",
    "FixtureManifest",
    "FixtureReplayError",
    "FixtureReplayPackage",
    "FixtureReplayPreparation",
    "FixtureReplayRun",
    "load_d0_demo_fixture",
    "prepare_d0_demo_fixture_replay",
    "run_d0_demo_fixture_replay",
]
from .run import (
    ResearchRunArtifacts,
    ResearchRunRecord,
    execute_research_ask,
    load_research_run,
)
from .company_context import (
    CitedCompanyStatement,
    CompanyContextArtifact,
    CompanyContextSource,
    CompanyContextStore,
    CompanySlotObservation,
    CompanySlotResolution,
    ContextSourceType,
    OntologyUseMode,
    SlotState,
    build_company_context,
)
from .news_signal import (
    NEWS_SIGNAL_CALCULATION_VERSION,
    NewsCoverageQuality,
    NewsSignal,
    NewsSignalInputReceipt,
    NewsSignalItem,
    NewsSignalPolicy,
    NewsTopicDistribution,
    aggregate_news_signal,
)
from .research_evidence import (
    RESEARCH_EVIDENCE_SCHEMA_VERSION,
    CompanyContextEvidence,
    ConsensusEvidence,
    EvidenceMissingness,
    EvidenceReceipt,
    HistoricalAnalyticsEvidence,
    ManagementEvidence,
    MissingnessKind,
    NewsEvidence,
    OntologyCoverage,
    PriorContextCardEvidence,
    ResearchEvidenceBundle,
    ResearchEvidenceStore,
    TargetEventIdentity,
    TechnicalEvidence,
    TechnicalProjection,
    assemble_research_evidence_bundle,
    research_evidence_json_schema,
)
from .synthesis_input import (
    ResearchEvidenceSource,
    external_bundle_from_research_evidence,
)
from .mvp_acceptance import (
    MvpAcceptanceResult,
    MvpAcceptanceStatus,
    audit_mvp_outcome,
)

__all__ = [
    "ResearchRunArtifacts",
    "ResearchRunRecord",
    "execute_research_ask",
    "load_research_run",
    "CitedCompanyStatement",
    "CompanyContextArtifact",
    "CompanyContextSource",
    "CompanyContextStore",
    "CompanySlotObservation",
    "CompanySlotResolution",
    "ContextSourceType",
    "OntologyUseMode",
    "SlotState",
    "build_company_context",
    "NEWS_SIGNAL_CALCULATION_VERSION",
    "NewsCoverageQuality",
    "NewsSignal",
    "NewsSignalInputReceipt",
    "NewsSignalItem",
    "NewsSignalPolicy",
    "NewsTopicDistribution",
    "aggregate_news_signal",
    "RESEARCH_EVIDENCE_SCHEMA_VERSION", "CompanyContextEvidence", "ConsensusEvidence",
    "EvidenceMissingness", "EvidenceReceipt", "HistoricalAnalyticsEvidence", "ManagementEvidence",
    "MissingnessKind", "NewsEvidence", "OntologyCoverage", "PriorContextCardEvidence",
    "ResearchEvidenceBundle", "ResearchEvidenceStore", "TargetEventIdentity", "TechnicalEvidence",
    "TechnicalProjection", "assemble_research_evidence_bundle", "research_evidence_json_schema",
    "ResearchEvidenceSource", "external_bundle_from_research_evidence",
    "MvpAcceptanceResult", "MvpAcceptanceStatus", "audit_mvp_outcome",
]

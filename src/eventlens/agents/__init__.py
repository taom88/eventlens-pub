"""Constrained, evidence-grounded analyst interpretation."""

from .bootstrap import DEFAULT_SKILLS, DEFAULT_TOOLS, ReasoningProviderSettings, bootstrap_reasoning_adapter
from .models import (
    AgentErrorCode, AgentFailure, AgentReasoningResult, AgentSkill, AgentTool, AnalystInterpretationRequest,
    AnalystReport, Citation, ClaimKind, EvidenceClaim, EvidenceItem, EvidenceOrigin, ReportKind,
    validate_report_evidence,
)
from .reasoning import DeepSeekReasoningAdapter, DeterministicFixtureReasoningAdapter, DurableCachedReasoningAdapter, MoonshotKimiReasoningAdapter, OpenAICompatibleReasoningAdapter, ReasoningAdapter
from .tools import build_transcript_evidence
from .context_card_audit import ContextCardAuditor, DeterministicContextCardAuditor
from .external import (
    EXTERNAL_AGENT_SCHEMA_VERSION,
    Abstention,
    Capability,
    CanonicalMetric,
    ExternalAgentBundle,
    ExternalAgentReport,
    ExternalClaim,
    ExternalEvidence,
    EvidenceRetentionStatus,
    EvidenceRightsStatus,
    ExternalRecommendation,
    ExternalScenario,
    HistoricalContextRef,
    ReactionForecast,
    RecommendationAction,
    Route,
    UsageReceipt,
    ValuationZone,
    external_agent_json_schema,
    external_agent_report_json_schema,
)
from .external_bundle import assemble_external_bundle
from .external_handoff import ExternalAgentHandoffStore, ExternalHandoffState, ExternalHandoffStatus
from .external_validation import (
    ExternalReportIssueCode,
    ExternalReportValidationError,
    ValidatedExternalReport,
    evidence_id,
    parse_external_report_json,
    validate_external_report,
)
from .external_reasoning import (
    DeepSeekExternalAgentAdapter,
    ExternalReasoningError,
    ExternalReasoningErrorCode,
    run_configured_agent_route,
)
from .external_fixture import DeterministicExternalAgentFixture
from .external_rendering import render_external_report_markdown
from .news import (
    CONTENT_CLAIM_EXTRACTOR_VERSION,
    NEWS_AGENT_SKILLS,
    NEWS_CLASSIFICATION_SCHEMA_VERSION,
    NEWS_MATERIALITY_AND_SENTIMENT_VERSION,
    NEWS_SKILL_DESCRIPTORS,
    NewsClassificationRequest,
    NewsClassificationResult,
    NewsClassifiedClaim,
    NewsEntityMatch,
    NewsEventRelationship,
    NewsHorizon,
    NewsImplication,
    NewsMateriality,
    NewsNovelty,
    NewsPassageCitation,
    NewsProviderNativeSentiment,
    NewsRelevance,
    NewsSkillDescriptor,
    NewsSummaryCitation,
    NewsTone,
    NewsTopic,
    NewsUsageReceipt,
    news_classification_json_schema,
)

__all__ = [
    "DEFAULT_SKILLS", "DEFAULT_TOOLS", "ReasoningProviderSettings", "bootstrap_reasoning_adapter",
    "AgentErrorCode", "AgentFailure", "AgentReasoningResult", "AgentSkill", "AgentTool",
    "AnalystInterpretationRequest", "AnalystReport", "Citation", "ClaimKind", "EvidenceClaim",
    "EvidenceItem", "EvidenceOrigin", "ReportKind", "validate_report_evidence",
    "DeepSeekReasoningAdapter", "DeterministicFixtureReasoningAdapter", "MoonshotKimiReasoningAdapter",
    "OpenAICompatibleReasoningAdapter", "ReasoningAdapter",
    "DurableCachedReasoningAdapter",
    "build_transcript_evidence",
    "ContextCardAuditor", "DeterministicContextCardAuditor",
    "EXTERNAL_AGENT_SCHEMA_VERSION", "Abstention", "ExternalAgentBundle",
    "ExternalAgentReport", "ExternalClaim", "ExternalEvidence", "CanonicalMetric", "Capability",
    "EvidenceRetentionStatus", "EvidenceRightsStatus",
    "ExternalRecommendation", "ExternalScenario", "HistoricalContextRef", "ReactionForecast",
    "Route", "RecommendationAction", "UsageReceipt", "ValuationZone",
    "external_agent_json_schema",
    "external_agent_report_json_schema",
    "assemble_external_bundle",
    "ExternalAgentHandoffStore", "ExternalHandoffState", "ExternalHandoffStatus",
    "ExternalReportIssueCode", "ExternalReportValidationError",
    "ValidatedExternalReport", "evidence_id", "parse_external_report_json",
    "validate_external_report",
    "DeepSeekExternalAgentAdapter", "ExternalReasoningError",
    "ExternalReasoningErrorCode", "run_configured_agent_route",
    "DeterministicExternalAgentFixture",
    "render_external_report_markdown",
    "CONTENT_CLAIM_EXTRACTOR_VERSION", "NEWS_AGENT_SKILLS", "NEWS_CLASSIFICATION_SCHEMA_VERSION",
    "NEWS_MATERIALITY_AND_SENTIMENT_VERSION", "NEWS_SKILL_DESCRIPTORS", "NewsClassificationRequest",
    "NewsClassificationResult", "NewsClassifiedClaim", "NewsEntityMatch", "NewsEventRelationship",
    "NewsHorizon", "NewsImplication", "NewsMateriality", "NewsNovelty", "NewsPassageCitation",
    "NewsProviderNativeSentiment", "NewsRelevance", "NewsSkillDescriptor", "NewsTone", "NewsTopic",
    "NewsSummaryCitation", "NewsUsageReceipt", "news_classification_json_schema",
]

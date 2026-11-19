import argparse

import eventlens.app as app
import eventlens.graph as graph
import eventlens.research_engines as research_engines
from eventlens.app.commands import build_parser


def test_cli_has_one_product_lifecycle_surface() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set(subparsers.choices)

    assert {"demo", "research-ask", "research-import-agent-report"} <= commands
    assert commands.isdisjoint(
        {
            "workflow-start",
            "workflow-resume",
            "workflow-demo",
            "workflow-ask",
            "workflow-report",
            "mocked-mcp-demo",
        }
    )


def test_legacy_packages_export_models_but_no_transition_writer() -> None:
    assert graph.WorkflowState.__name__ == "WorkflowState"
    assert not hasattr(graph, "EventLensPreEarningsWorkflow")
    assert not hasattr(graph, "open_durable_pre_earnings_graph")
    assert app.__all__ == []


def test_vibe_has_one_allowlisted_evidence_adapter() -> None:
    assert research_engines.VibeMcpEvidenceClient.__name__ == "VibeMcpEvidenceClient"
    assert not hasattr(research_engines, "VibeTradingMcpMarketDataEngine")
    assert not hasattr(research_engines, "DeterministicMockVibeTradingEngine")

from pathlib import Path

from eventlens.learning.context_card_grading import load_context_card_fixture, grade_retrieval


def test_retrieval_metrics_and_rates():
    root = Path(__file__).parents[2] / "fixtures/evals/context_cards"
    metrics = {load_context_card_fixture(p).fixture_id: grade_retrieval(load_context_card_fixture(p)) for p in root.glob("*.json")}
    assert metrics["growth_basis"].tier_1_coverage == 1
    assert metrics["growth_basis"].tier_2_coverage == 1
    assert metrics["boilerplate"].boilerplate_rate == 0
    assert metrics["boilerplate"].truncation_rate == 0


def test_missing_critical_claim_identifies_exact_field():
    case = load_context_card_fixture(Path(__file__).parents[2] / "fixtures/evals/context_cards/growth_basis.json")
    altered = case.model_copy(update={"ranked_candidates": ({"claim_id": "growth_cc"},)})
    result = grade_retrieval(altered)
    assert "retrieval.missing_tier1.growth_reported" in result.failures

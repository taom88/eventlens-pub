from pathlib import Path

from eventlens.learning.context_card_grading import load_context_card_fixture, grade_interpretations


def test_adversarial_interpretation_cases_pass_gold_labels():
    root = Path(__file__).parents[2] / "fixtures/evals/context_cards"
    for path in root.glob("*.json"):
        case = load_context_card_fixture(path)
        assert grade_interpretations(case) == (True, ())


def test_interpretation_failure_names_question_and_field():
    case = load_context_card_fixture(Path(__file__).parents[2] / "fixtures/evals/context_cards/late_consensus.json")
    altered = case.model_copy(update={"interpretation_cases": (case.interpretation_cases[0].model_copy(update={"observed": {"cutoff_safe": True, "reconstructed": True}}),)})
    passed, failures = grade_interpretations(altered)
    assert not passed
    assert "late_consensus.cutoff.cutoff_safe" in failures

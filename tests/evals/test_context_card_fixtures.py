from pathlib import Path

from eventlens.learning.context_card_grading import load_context_card_fixture


ROOT = Path(__file__).parents[2] / "fixtures/evals/context_cards"


def test_all_phase_47_fixtures_are_compact_and_parseable():
    names = {"gaap_gain", "late_consensus", "growth_basis", "approximate_guidance", "weak_guidance_reaction", "boilerplate"}
    cases = [load_context_card_fixture(path) for path in sorted(ROOT.glob("*.json"))]
    assert {case.fixture_id for case in cases} == names
    assert all(path.stat().st_size < 4096 for path in ROOT.glob("*.json"))


def test_adversarial_fixture_semantics_are_explicit():
    cases = {case.fixture_id: case for case in (load_context_card_fixture(p) for p in ROOT.glob("*.json"))}
    assert cases["gaap_gain"].interpretation_cases[0].expected["normalized_surprise_blocked"] is True
    assert cases["late_consensus"].interpretation_cases[0].expected["cutoff_safe"] is False
    assert cases["weak_guidance_reaction"].interpretation_cases[0].expected["guidance_candidate"] is True

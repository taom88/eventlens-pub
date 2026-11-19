from __future__ import annotations

from eventlens.context_cards.transcripts import (
    AnswerType,
    SpeakerRole,
    TranscriptSection,
    parse_transcript,
)
from eventlens.providers.models import TranscriptPassage, TranscriptRecord


def _record(*passages: TranscriptPassage) -> TranscriptRecord:
    return TranscriptRecord(ticker="DEMO", fiscal_quarter="2026Q2", passages=passages)


def test_parser_preserves_turns_and_normalizes_titles_and_sections() -> None:
    parsed = parse_transcript(_record(
        TranscriptPassage(passage_id="p0", speaker="Satya Nadella", role="Chief Executive Officer", text="Welcome."),
        TranscriptPassage(passage_id="p1", speaker="Amy Hood", role="CFO", text="We will now begin the question-and-answer session."),
        TranscriptPassage(passage_id="p2", speaker="Jane Doe", role="Equity Research Analyst, Contoso Securities", text="What is your fiscal Q4 revenue outlook?"),
        TranscriptPassage(passage_id="p3", speaker="Amy Hood", role="Chief Financial Officer", text="For Q4, we expect revenue growth of 10%."),
    ))

    assert [turn.passage_id for turn in parsed.turns] == ["p0", "p1", "p2", "p3"]
    assert parsed.turns[0].speaker_role is SpeakerRole.CEO
    assert parsed.turns[1].speaker_role is SpeakerRole.CFO
    assert parsed.turns[0].section is TranscriptSection.PREPARED_REMARKS
    assert all(turn.section is TranscriptSection.QUESTION_AND_ANSWER for turn in parsed.turns[1:])
    assert len(parsed.question_answer_pairs) == 1
    pair = parsed.question_answer_pairs[0]
    assert pair.question.analyst_name == "Jane Doe"
    assert pair.question.analyst_firm == "Contoso Securities"
    assert pair.answers[0].answer_type is AnswerType.DIRECT


def test_parser_classifies_partial_and_explicit_deflection_conservatively() -> None:
    parsed = parse_transcript(_record(
        TranscriptPassage(passage_id="q1", speaker="Analyst", role="Analyst", section="Q&A", text="What is your cloud margin outlook?"),
        TranscriptPassage(passage_id="a1", speaker="CFO", role="CFO", section="Q&A", text="Demand remains healthy across our cloud business."),
        TranscriptPassage(passage_id="q2", speaker="Analyst", role="Analyst", section="Q&A", text="Can you provide a full-year capex forecast?"),
        TranscriptPassage(passage_id="a2", speaker="CFO", role="CFO", section="Q&A", text="We are not providing a full-year capex forecast today."),
    ))

    assert parsed.question_answer_pairs[0].answers[0].answer_type is AnswerType.PARTIAL
    assert parsed.question_answer_pairs[1].answers[0].answer_type is AnswerType.DEFLECTION


def test_parser_keeps_unknown_identity_explicit_and_flags_numeric_guidance() -> None:
    parsed = parse_transcript(_record(
        TranscriptPassage(passage_id="unknown", section="prepared remarks", text="Our revenue guidance calls for 12% growth next quarter."),
    ))

    assert parsed.turns[0].speaker_role is SpeakerRole.UNKNOWN
    assert parsed.turns[0].flags == ("unresolved_management_speaker_numeric_guidance",)
    assert parsed.warnings == ("unknown:unresolved_management_speaker_numeric_guidance",)


def test_parser_does_not_drop_an_answer_question_link() -> None:
    parsed = parse_transcript(_record(
        TranscriptPassage(passage_id="question", speaker="Analyst", role="Analyst", section="Q&A", text="What is the outlook for capacity?"),
        TranscriptPassage(passage_id="answer-1", speaker="CEO", role="CEO", section="Q&A", text="We are adding capacity steadily."),
        TranscriptPassage(passage_id="answer-2", speaker="CFO", role="CFO", section="Q&A", text="The investment continues through the year."),
    ))

    pair = parsed.question_answer_pairs[0]
    assert pair.question.passage_id == "question"
    assert [answer.passage_id for answer in pair.answers] == ["answer-1", "answer-2"]

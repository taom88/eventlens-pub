"""Deterministic, lossless transcript structure for v4 context cards.

This module normalizes labels supplied by a provider; it never guesses a
speaker's identity, supplies missing content, or interprets market reaction.
Every normalized turn retains its original ``passage_id`` and text so a receipt
can always be resolved back to the provider-native passage.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Iterable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from eventlens.providers.models import TranscriptPassage, TranscriptRecord


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _TranscriptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SpeakerRole(StrEnum):
    CEO = "ceo"
    CFO = "cfo"
    COO = "coo"
    PRESIDENT = "president"
    INVESTOR_RELATIONS = "investor_relations"
    EXECUTIVE = "executive"
    ANALYST = "analyst"
    OPERATOR = "operator"
    UNKNOWN = "unknown"


class TranscriptSection(StrEnum):
    PREPARED_REMARKS = "prepared_remarks"
    QUESTION_AND_ANSWER = "question_and_answer"
    UNKNOWN = "unknown"


class AnswerType(StrEnum):
    DIRECT = "direct"
    PARTIAL = "partial"
    DEFLECTION = "deflection"
    UNKNOWN = "unknown"


class TranscriptTurn(_TranscriptModel):
    """A source-addressable turn, in original transcript order."""

    turn_index: int = Field(ge=0)
    passage_id: NonEmptyStr
    text: NonEmptyStr
    speaker_name: NonEmptyStr | None = None
    speaker_title: NonEmptyStr | None = None
    company: NonEmptyStr | None = None
    firm: NonEmptyStr | None = None
    speaker_role: SpeakerRole = SpeakerRole.UNKNOWN
    section: TranscriptSection = TranscriptSection.UNKNOWN
    flags: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def flags_are_unique(self) -> "TranscriptTurn":
        if len(set(self.flags)) != len(self.flags):
            raise ValueError("transcript turn flags must be unique")
        return self


class TranscriptQuestion(_TranscriptModel):
    turn_index: int = Field(ge=0)
    passage_id: NonEmptyStr
    text: NonEmptyStr
    analyst_name: NonEmptyStr | None = None
    analyst_title: NonEmptyStr | None = None
    analyst_firm: NonEmptyStr | None = None


class TranscriptAnswer(_TranscriptModel):
    turn_index: int = Field(ge=0)
    passage_id: NonEmptyStr
    text: NonEmptyStr
    speaker_name: NonEmptyStr | None = None
    speaker_title: NonEmptyStr | None = None
    company: NonEmptyStr | None = None
    firm: NonEmptyStr | None = None
    speaker_role: SpeakerRole = SpeakerRole.UNKNOWN
    answer_type: AnswerType = AnswerType.UNKNOWN
    flags: tuple[NonEmptyStr, ...] = ()


class QuestionAnswerPair(_TranscriptModel):
    """An analyst question and its following management answer turns."""

    question: TranscriptQuestion
    answers: tuple[TranscriptAnswer, ...] = ()


class TranscriptParseResult(_TranscriptModel):
    """Parsed transcript plus explicit review warnings, never hidden guesses."""

    turns: tuple[TranscriptTurn, ...]
    question_answer_pairs: tuple[QuestionAnswerPair, ...] = ()
    warnings: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def turns_are_ordered_and_addressable(self) -> "TranscriptParseResult":
        indexes = tuple(turn.turn_index for turn in self.turns)
        if indexes != tuple(range(len(self.turns))):
            raise ValueError("transcript turns must preserve contiguous source order")
        passage_ids = tuple(turn.passage_id for turn in self.turns)
        if len(set(passage_ids)) != len(passage_ids):
            raise ValueError("transcript turn passage ids must be unique")
        known = set(passage_ids)
        for pair in self.question_answer_pairs:
            if pair.question.passage_id not in known:
                raise ValueError("Q&A question must resolve to a transcript turn")
            if any(answer.passage_id not in known for answer in pair.answers):
                raise ValueError("Q&A answer must resolve to a transcript turn")
        return self


_QA_MARKERS = re.compile(
    r"\b(?:question(?:s)?(?:[- ]and[- ]answer)? session|q\s*&\s*a|"
    r"we will now (?:begin|take).*question|open.*line.*question)\b",
    re.IGNORECASE,
)
_DEFLECTION = re.compile(
    r"\b(?:not (?:going to|able to|in a position to)|cannot|can't|won't|decline to|"
    r"not provid(?:e|ing)|not disclos(?:e|ing)|too early to)\b",
    re.IGNORECASE,
)
_GUIDANCE_WORDS = re.compile(
    r"\b(?:guidance|outlook|expect(?:ation|ed|s)?|forecast|revenue|margin|capex|"
    r"capital expenditure|growth)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?:\$\s*\d|\d[\d,.]*\s*(?:%|bps?|basis points?|million|billion|bn|m))", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")
_TOPIC_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "be", "by", "can", "could", "do", "for", "from",
    "how", "i", "in", "is", "it", "of", "on", "or", "please", "the", "this", "to",
    "was", "what", "will", "with", "would", "you", "your",
})


def parse_transcript(transcript: TranscriptRecord | Iterable[TranscriptPassage]) -> TranscriptParseResult:
    """Parse a transcript with conservative rules and no inference service.

    A provider's explicit ``section`` wins.  Otherwise, the parser changes to
    Q&A only after a conventional operator marker.  Analyst turns in Q&A begin
    a question; following management (or unresolved) turns are attached to it.
    """

    passages = tuple(transcript.passages if isinstance(transcript, TranscriptRecord) else transcript)
    turns: list[TranscriptTurn] = []
    section = TranscriptSection.PREPARED_REMARKS
    for index, passage in enumerate(passages):
        explicit_section = _normalize_section(passage.section)
        if explicit_section is not TranscriptSection.UNKNOWN:
            section = explicit_section
        elif section is not TranscriptSection.QUESTION_AND_ANSWER and _QA_MARKERS.search(passage.text):
            section = TranscriptSection.QUESTION_AND_ANSWER
        role = _normalize_role(passage.speaker, passage.role)
        flags: list[str] = []
        if role is SpeakerRole.UNKNOWN and _is_numeric_guidance(passage.text) and (
            section is TranscriptSection.PREPARED_REMARKS or section is TranscriptSection.QUESTION_AND_ANSWER
        ):
            flags.append("unresolved_management_speaker_numeric_guidance")
        turns.append(TranscriptTurn(
            turn_index=index, passage_id=passage.passage_id, text=passage.text,
            speaker_name=passage.speaker, speaker_title=passage.role,
            company=passage.company, firm=passage.firm or _firm_from_title(passage.role),
            speaker_role=role, section=section, flags=tuple(flags),
        ))

    pairs: list[QuestionAnswerPair] = []
    current_question: TranscriptQuestion | None = None
    current_answers: list[TranscriptAnswer] = []

    def close_pair() -> None:
        nonlocal current_question, current_answers
        if current_question is not None:
            pairs.append(QuestionAnswerPair(question=current_question, answers=tuple(current_answers)))
        current_question, current_answers = None, []

    for turn in turns:
        if turn.section is not TranscriptSection.QUESTION_AND_ANSWER:
            close_pair()
            continue
        if turn.speaker_role is SpeakerRole.ANALYST:
            close_pair()
            current_question = TranscriptQuestion(
                turn_index=turn.turn_index, passage_id=turn.passage_id, text=turn.text,
                analyst_name=turn.speaker_name, analyst_title=turn.speaker_title,
                analyst_firm=turn.firm,
            )
        elif turn.speaker_role is not SpeakerRole.OPERATOR and current_question is not None:
            current_answers.append(TranscriptAnswer(
                turn_index=turn.turn_index, passage_id=turn.passage_id, text=turn.text,
                speaker_name=turn.speaker_name, speaker_title=turn.speaker_title,
                company=turn.company, firm=turn.firm,
                speaker_role=turn.speaker_role,
                answer_type=_classify_answer(current_question.text, turn.text), flags=turn.flags,
            ))
    close_pair()
    warnings = tuple(
        f"{turn.passage_id}:unresolved_management_speaker_numeric_guidance"
        for turn in turns if "unresolved_management_speaker_numeric_guidance" in turn.flags
    )
    return TranscriptParseResult(turns=tuple(turns), question_answer_pairs=tuple(pairs), warnings=warnings)


def _normalize_section(value: str | None) -> TranscriptSection:
    text = (value or "").strip().lower().replace("_", " ")
    if "question" in text or "q&a" in text or "q & a" in text:
        return TranscriptSection.QUESTION_AND_ANSWER
    if "prepared" in text or "remark" in text or "presentation" in text:
        return TranscriptSection.PREPARED_REMARKS
    return TranscriptSection.UNKNOWN


def _normalize_role(speaker: str | None, title: str | None) -> SpeakerRole:
    label = f"{speaker or ''} {title or ''}".lower()
    if "operator" in label or "conference call coordinator" in label:
        return SpeakerRole.OPERATOR
    if "analyst" in label or "research" in label:
        return SpeakerRole.ANALYST
    if "chief executive" in label or re.search(r"\bceo\b", label):
        return SpeakerRole.CEO
    if "chief financial" in label or re.search(r"\bcfo\b", label):
        return SpeakerRole.CFO
    if "chief operating" in label or re.search(r"\bcoo\b", label):
        return SpeakerRole.COO
    if "investor relations" in label or re.search(r"\bir\b", label):
        return SpeakerRole.INVESTOR_RELATIONS
    if "president" in label:
        return SpeakerRole.PRESIDENT
    if any(word in label for word in ("chief", "executive vice president", "vice president", "general manager")):
        return SpeakerRole.EXECUTIVE
    return SpeakerRole.UNKNOWN


def _firm_from_title(title: str | None) -> str | None:
    if not title:
        return None
    match = re.search(r"\b(?:analyst|research)\s*(?:,|-|at)\s*(.+)$", title, re.IGNORECASE)
    return match.group(1).strip(" ,-") if match and match.group(1).strip(" ,-") else None


def _is_numeric_guidance(text: str) -> bool:
    return bool(_NUMBER.search(text) and _GUIDANCE_WORDS.search(text))


def _classify_answer(question: str, answer: str) -> AnswerType:
    if _DEFLECTION.search(answer):
        return AnswerType.DEFLECTION
    question_words = set(_WORD.findall(question.lower())) - _TOPIC_STOPWORDS
    answer_words = set(_WORD.findall(answer.lower())) - _TOPIC_STOPWORDS
    if (_NUMBER.search(question) and _NUMBER.search(answer)) or len(question_words & answer_words) >= 2:
        return AnswerType.DIRECT
    return AnswerType.PARTIAL if answer.strip() else AnswerType.UNKNOWN


__all__ = [
    "AnswerType", "QuestionAnswerPair", "SpeakerRole", "TranscriptAnswer",
    "TranscriptParseResult", "TranscriptQuestion", "TranscriptSection", "TranscriptTurn",
    "parse_transcript",
]

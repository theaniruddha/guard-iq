from __future__ import annotations

"""
Log ingest + problem detection.

Input format: JSONL, one chat turn per line:

    {"conversation_id": "c1", "role": "user", "text": "...", "ts": "2026-07-01T10:00:00"}

Optional per-turn fields:
    "feedback": "negative"       explicit thumbs-down on an assistant turn
    "flag": "bad_response"       operator-marked problem

A ProblemEvent is a user ask that the bot visibly failed to handle:
the following assistant turn deflected/apologised, the user pushed back,
the turn was explicitly flagged, or the user asked for a human.
These events are what warm-start clusters and turns into draft rules.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Assistant-side deflection / non-answer phrases
_DEFLECTION = [
    r"\bi (can't|cannot|can not)\b",
    r"\bi('m| am) (not able|unable)\b",
    r"\bi don't (have|know)\b",
    r"\bi('m| am) not sure\b",
    r"\bunfortunately\b",
    r"\bsorry\b.{0,40}\b(can't|cannot|unable|don't)\b",
    r"\bthat('s| is) (outside|beyond) (my|what)\b",
    r"\bi (do not|don't) have access\b",
]

# User-side frustration / pushback phrases (looked for in the NEXT user turn)
_FRUSTRATION = [
    r"\bnot what i asked\b",
    r"\byou (already|just) (said|told)\b",
    r"\b(doesn't|does not|didn't) help\b",
    r"\buseless\b",
    r"\btalk to (a )?(human|person|agent|someone|representative)\b",
    r"\breal person\b",
    r"\bthis is (ridiculous|frustrating)\b",
    r"\basked (you )?(this )?(already|before|twice|again)\b",
    r"\bwhy (can't|won't) you\b",
]

_DEFLECTION_RE = [re.compile(p, re.IGNORECASE) for p in _DEFLECTION]
_FRUSTRATION_RE = [re.compile(p, re.IGNORECASE) for p in _FRUSTRATION]


@dataclass
class Turn:
    conversation_id: str
    role: str  # "user" | "assistant"
    text: str
    ts: str | None = None
    feedback: str | None = None
    flag: str | None = None


@dataclass
class ProblemEvent:
    conversation_id: str
    user_text: str            # the ask that went unhandled — this is what gets clustered
    assistant_text: str       # what the bot said instead
    signals: list[str] = field(default_factory=list)  # why we think it's a problem

    @property
    def key_text(self) -> str:
        return self.user_text


def load_turns(path: str | Path) -> list[Turn]:
    turns: list[Turn] = []
    for line_no, line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON ({exc})") from exc
        turns.append(
            Turn(
                conversation_id=str(row.get("conversation_id", "unknown")),
                role=row.get("role", "user"),
                text=row.get("text", ""),
                ts=row.get("ts"),
                feedback=row.get("feedback"),
                flag=row.get("flag"),
            )
        )
    return turns


def _group_conversations(turns: list[Turn]) -> dict[str, list[Turn]]:
    convs: dict[str, list[Turn]] = {}
    for t in turns:
        convs.setdefault(t.conversation_id, []).append(t)
    return convs


def _signals_for(user: Turn, assistant: Turn | None, next_user: Turn | None) -> list[str]:
    # A user turn with no reply at all (end of transcript) is not evidence of
    # a problem — it's usually the user leaving. Only judge answered turns.
    if assistant is None:
        return []
    signals: list[str] = []
    if any(p.search(assistant.text) for p in _DEFLECTION_RE):
        signals.append("assistant_deflected")
    if assistant.feedback == "negative" or assistant.flag:
        signals.append("explicitly_flagged")
    if len(assistant.text.strip()) < 15:
        signals.append("empty_or_thin_reply")
    if next_user is not None and any(p.search(next_user.text) for p in _FRUSTRATION_RE):
        signals.append("user_pushed_back")
    return signals


def extract_problem_events(turns: list[Turn]) -> list[ProblemEvent]:
    """Walk each conversation; emit an event for every user turn whose
    handling shows a failure signal."""
    events: list[ProblemEvent] = []
    for conv in _group_conversations(turns).values():
        for i, turn in enumerate(conv):
            if turn.role != "user":
                continue
            assistant = next((t for t in conv[i + 1 :] if t.role == "assistant"), None)
            next_user = next((t for t in conv[i + 1 :] if t.role == "user"), None)
            signals = _signals_for(turn, assistant, next_user)
            if signals:
                events.append(
                    ProblemEvent(
                        conversation_id=turn.conversation_id,
                        user_text=turn.text,
                        assistant_text=assistant.text if assistant else "",
                        signals=signals,
                    )
                )
    return events

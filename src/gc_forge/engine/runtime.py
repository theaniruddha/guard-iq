from __future__ import annotations

"""
Guardrail Contract Engine — runtime (the dumb/fast part)

Design goals (lean & simple):
- Only approved/edited rules matter.
- Pure functions. No LLM, no network (except optional presidio at startup).
- Match on user text (input) or model draft (output).
- Keyword + quoted phrase + a few hardcoded abuse patterns + delegated PII.
- Keep code easy to read and audit. Deliberately not smart.

This file + the committed Policy YAML is all you need to enforce.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from gc_forge.contract.schema import Bucket, Policy, ReviewStatus, Rule, Side
from gc_forge.store.versioning import load as load_policy

BLOCK_MESSAGE = "I can't help with that request."

_ABUSE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bjailbreak\b",
        r"\bignore (all|your) (previous|prior) instructions\b",
        r"\bDAN\b",
        r"\bdo anything now\b",
        r"\bpretend you (are|have) no (rules|restrictions)\b",
        r"\bdisable (your )?safety\b",
        r"\byou are now (an? )?(unrestricted|unfiltered|uncensored|jailbroken)\b",
        r"\b(bypass|circumvent|get around|work around|turn off|override|disregard)\b"
        r".{0,30}\b(rules?|safety|restrictions?|guardrails?|filters?|protocols?|instructions?|protections?|polic(y|ies)|safeguards?)\b",
        r"\bignore (the |all |your )?(rules|guidelines|restrictions|instructions)\b",
        r"\b(act|roleplay|pretend) as (an? )?(unrestricted|unfiltered|uncensored)\b",
    )
]

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "for",
        "to",
        "of",
        "in",
        "on",
        "with",
        "that",
        "this",
        "user",
        "requests",
        "request",
        "detect",
        "catch",
        "block",
        "when",
        "where",
        "from",
        "into",
        "about",
        "should",
        "must",
        "will",
        "would",
        "could",
        "model",
        "reply",
        "message",
        "attempts",
        "attempt",
        "asking",
        "asked",
        "provide",
        "provides",
        "offers",
        "offer",
        "makes",
        "make",
    }
)

_presidio_analyzer = None

# Zero-width / invisible characters used to split trigger words ("jail​break")
_ZERO_WIDTH_RE = re.compile("[\\u200b\\u200c\\u200d\\u2060\\ufeff\\u00ad]")

# Common shorthand -> Presidio's canonical entity labels, so a contract that
# says "presidio:EMAIL,PHONE" still activates the right recognizers.
_PRESIDIO_ALIASES = {
    "EMAIL": "EMAIL_ADDRESS",
    "PHONE": "PHONE_NUMBER",
    "NAME": "PERSON",
    "CREDITCARD": "CREDIT_CARD",
    "SSN": "US_SSN",
    "IP": "IP_ADDRESS",
}


def _normalize(text: str) -> str:
    """Defeat cheap evasion before any matching: fullwidth/compatibility
    forms (NFKC), zero-width joiners inside words, repeated whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    return re.sub(r"\s+", " ", text)


@dataclass(frozen=True)
class Decision:
    bucket: Bucket
    message: Optional[str]
    route_to: Optional[str]
    matched_rule_id: str


class PassDecision:
    """Sentinel returned when no rule fires."""

    def __repr__(self) -> str:
        return "PASS"


PASS = PassDecision()


def load(path: str) -> Policy:
    return load_policy(path)


def _active_rules(policy: Policy, side: Side) -> list[Rule]:
    """Return only rules that have passed human review for this side.
    A rule whose own side disagrees with the list it sits in never runs."""
    rules = policy.input_rules if side == Side.input else policy.output_rules
    return [
        r
        for r in rules
        if r.side == side and r.review_status in (ReviewStatus.approved, ReviewStatus.edited)
    ]


def _keywords_from_intent(intent: str) -> list[str]:
    """Pull meaningful keywords out of a rule's intent description.
    We ignore very common words (stopwords) and short tokens.
    """
    tokens = re.findall(r"[a-z0-9]+", intent.lower())
    return [t for t in tokens if len(t) > 3 and t not in _STOPWORDS]


def _keyword_in_text(keyword: str, text: str, words: list[str]) -> bool:
    """Very small fuzzy match: word-boundary prefix or simple 5-char stem.
    Enough for 'refund' vs 'refunds' without full NLP, without firing
    mid-word ('rate' must not match 'grateful').
    """
    if re.search(rf"\b{re.escape(keyword)}", text):
        return True
    stem = keyword[: min(5, len(keyword))]
    return any(w.startswith(stem) or stem.startswith(w[: min(5, len(w))]) for w in words if len(w) > 3)


def _intent_matches(text: str, intent: str) -> bool:
    """Core matching used by the engine.

    1. Respect explicit quoted phrases from the rule writer ("money back").
    2. Require a minimum number of non-trivial keywords to fire (reduces noise).
    """
    lowered = text.lower()
    words = re.findall(r"[a-z0-9]+", lowered)

    # Quoted phrases win immediately. Single quotes only count when they are
    # real delimiters, not apostrophes inside words ("user's", "don't").
    quoted = re.findall(r"\"([^\"]+)\"|(?<!\w)'([^']+)'(?!\w)", intent)
    for groups in quoted:
        phrase = next(g for g in groups if g)
        if phrase.lower() in lowered:
            return True

    keywords = _keywords_from_intent(intent)
    if not keywords:
        return False

    hits = sum(1 for kw in keywords if _keyword_in_text(kw, lowered, words))
    # Need at least 2 (or all if fewer) keyword hits
    return hits >= min(2, len(keywords))


def _abuse_matches(text: str) -> bool:
    """Hard-coded high-confidence jailbreak / DAN patterns for block rules."""
    return any(p.search(text) for p in _ABUSE_PATTERNS)


def _get_presidio_analyzer():
    """Lazy singleton for optional Presidio.

    Returns None (treated as unavailable) if:
    - presidio not installed
    - spacy model missing and can't be auto-fetched (common in minimal envs / CI)
    - any other init error

    This keeps the engine usable for pure business rules even when PII delegation
    can't be activated at runtime.
    """
    global _presidio_analyzer
    if _presidio_analyzer is not None:
        return _presidio_analyzer if _presidio_analyzer is not False else None
    try:
        from presidio_analyzer import AnalyzerEngine

        _presidio_analyzer = AnalyzerEngine()
        return _presidio_analyzer
    except (SystemExit, Exception):  # default en_core_web_lg missing, etc.
        pass
    try:
        # Fall back to the small spaCy model if it's installed — much lighter
        # than en_core_web_lg and enough for pattern-based entities.
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        )
        _presidio_analyzer = AnalyzerEngine(nlp_engine=provider.create_engine())
        return _presidio_analyzer
    except (SystemExit, Exception):
        _presidio_analyzer = False
        return None


def _pii_matches(text: str, delegate: str) -> bool:
    """Only fires if the rule declares pii_delegate and Presidio finds one of the listed entities.
    The engine itself never decides what is PII.
    Any failure inside presidio -> treat as no match (graceful degradation).
    """
    if not delegate.startswith("presidio"):
        return False
    # "presidio" (bare) delegates all entities; "presidio:PERSON,EMAIL" restricts.
    entities = (
        [e.strip().upper() for e in delegate.split(":", 1)[1].split(",") if e.strip()]
        if ":" in delegate
        else []
    )
    entities = [_PRESIDIO_ALIASES.get(e, e) for e in entities]
    analyzer = _get_presidio_analyzer()
    if analyzer is None:
        return False
    try:
        results = analyzer.analyze(text=text, language="en", entities=entities or None)
        return len(results) > 0
    except Exception:
        return False


def _rule_matches(rule: Rule, text: str) -> bool:
    """Does this (already approved) rule fire on the given text?

    Order:
    - pii_delegate rules fire ONLY via delegation (never keyword-match PII —
      the engine must not detect PII itself, per the contract)
    - special fast-path for known abuse patterns on block rules
    - general intent/keyword match (the main path for business rules)
    """
    if rule.pii_delegate:
        return _pii_matches(text, rule.pii_delegate)

    if rule.bucket == Bucket.block and _abuse_matches(text):
        if "abuse" in rule.intent.lower() or "jailbreak" in rule.intent.lower():
            return True

    return _intent_matches(text, rule.intent)


def _decision_from_rule(rule: Rule) -> Decision:
    """Turn a matching rule into the action the caller should take."""
    if rule.bucket == Bucket.block:
        return Decision(
            bucket=rule.bucket,
            message=rule.message or BLOCK_MESSAGE,
            route_to=rule.route_to,
            matched_rule_id=rule.id,
        )
    if rule.bucket in (Bucket.steer, Bucket.graceful_reject):
        return Decision(
            bucket=rule.bucket,
            message=rule.message,
            route_to=rule.route_to,
            matched_rule_id=rule.id,
        )
    # pass_through should normally never reach here (we skip them)
    return Decision(
        bucket=rule.bucket,
        message=None,
        route_to=None,
        matched_rule_id=rule.id,
    )


def enforce(policy: Policy, text: str, side: Side | str) -> Decision | PassDecision:
    """Main entry point.

    Example:
        d = gc.enforce(policy, user_message, "input")
        if d is not gc.PASS:
            return d.message   # block or steer the user
    """
    if isinstance(side, str):
        side = Side(side)

    text = _normalize(text)
    rules = _active_rules(policy, side)

    # High severity first; at equal severity the hardest action wins
    # (block > steer > graceful_reject), then deterministic id order.
    severity_order = {"high": 0, "medium": 1, "low": 2}
    bucket_order = {"block": 0, "steer": 1, "graceful_reject": 2, "pass_through": 3}
    rules.sort(
        key=lambda r: (
            severity_order.get(r.severity.value, 9),
            bucket_order.get(r.bucket.value, 9),
            r.id,
        )
    )

    for rule in rules:
        if rule.bucket == Bucket.pass_through:
            continue
        if _rule_matches(rule, text):
            return _decision_from_rule(rule)

    return PASS
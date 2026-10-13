from __future__ import annotations

"""
Query hygiene + zero-result fallback for synthesis research.

The design rule: research by business TYPE, never a company name. Queries must
be broad on the SUBJECT (quick-service restaurant, not "Subway Brampton") and
specific on the RISK DIMENSION (allergen disclosure, promo/advertising rules).

Three layers, all deterministic:

1. banned_terms()/sanitize_query() — strip brand and city tokens that leaked in
   from the user's raw business/context input. Applied to every planned query
   before it is shown to the human or executed.
2. strip_jurisdiction() — fallback step 1 on a 0-result query: drop
   province/country terms. Jurisdiction is KEPT in first-pass queries (regional
   food-safety / advertising / employment law is real signal); it is only
   removed when it made the query too narrow to match anything.
3. broaden_vertical() — fallback step 2: generalize the vertical to its parent
   category (sandwich chain -> quick-service restaurant -> restaurant) so the
   risk dimension survives even if the niche wording has no coverage.

search_with_fallback() chains original -> jurisdiction-stripped -> broadened,
so a dimension is never dropped just because the first phrasing missed.
"""

import re
from typing import Callable

# Generic business words that may be capitalized in user input but are NOT
# brands/places ("Dental Clinic", "Pizza Restaurant").
_GENERIC_BUSINESS_WORDS = frozenset(
    """
    restaurant restaurants cafe coffee bakery bar pub bistro diner pizzeria pizza
    sandwich sandwiches burger sushi grill food service catering takeaway takeout
    dental dentist clinic clinics medical health healthcare pharmacy vet veterinary
    salon spa gym fitness yoga studio hotel motel hostel shop store retail boutique
    dealership garage repair auto car vehicle laundry cleaners bank insurance agency
    school daycare tutoring law legal accounting realty estate chain franchise
    company business small local online quick fast casual family
    """.split()
)

# Jurisdiction lexicon for the fallback (extend as needed). First-pass queries
# keep these; they are only stripped when a query returned nothing.
_JURISDICTIONS = frozenset(
    """
    canada canadian ontario quebec alberta manitoba saskatchewan
    british columbia nova scotia new brunswick newfoundland yukon
    usa us american california texas florida new york washington
    uk british england scotland wales ireland eu european europe
    australia australian india indian
    """.split()
)
_JURISDICTION_PHRASES = (
    "british columbia", "nova scotia", "new brunswick", "new york",
    "united states", "united kingdom", "european union",
)

# Niche vertical -> parent category. Longest match wins; plural tolerated.
_PARENT_VERTICAL: dict[str, str] = {
    "sandwich chain": "quick-service restaurant",
    "sandwich shop": "quick-service restaurant",
    "sub shop": "quick-service restaurant",
    "pizza restaurant": "quick-service restaurant",
    "pizzeria": "quick-service restaurant",
    "burger joint": "quick-service restaurant",
    "fast food restaurant": "quick-service restaurant",
    "quick-service restaurant": "restaurant",
    "quick service restaurant": "restaurant",
    "fast food": "restaurant",
    "coffee shop": "food service business",
    "bakery": "food service business",
    "food truck": "food service business",
    "restaurant": "food service business",
    "sandwich": "quick-service restaurant",
    "dental clinic": "healthcare clinic",
    "orthodontist": "dental clinic",
    "car dealership": "vehicle dealership",
    "used car lot": "vehicle dealership",
    "hair salon": "personal care business",
    "nail salon": "personal care business",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def banned_terms(business_type: str, context: str | None = None) -> set[str]:
    """Proper-noun-looking tokens from the user's raw input (brand, city).
    Lowercase result. Generic business words are never banned."""
    banned: set[str] = set()
    for raw in (business_type, context or ""):
        for tok in _WORD_RE.findall(raw):
            if tok[0].isupper() and tok.lower() not in _GENERIC_BUSINESS_WORDS:
                banned.add(tok.lower())
    return banned


def _tidy(query: str) -> str:
    """Clean up holes left by token removal."""
    query = re.sub(r"\s+", " ", query).strip()
    query = re.sub(r"^(and|or|for|in|of|the|under|near)\s+", "", query, flags=re.I)
    query = re.sub(r"\s+(and|or|for|in|of|the|under|near)$", "", query, flags=re.I)
    query = re.sub(r"\b(in|for|near|under)\s+(and|or|in|for|near|under)\b", r"\2", query, flags=re.I)
    return query.strip(" ,-")


def sanitize_query(query: str, banned: set[str], type_term: str | None = None) -> str:
    """Remove brand/city tokens. The first removed token is replaced with the
    type-level term (if known) so the query keeps a subject."""
    if not banned:
        return query
    replaced = False
    out: list[str] = []
    for tok in query.split():
        core = tok.strip(".,;:!?()\"'").lower()
        if core in banned:
            if type_term and not replaced:
                out.append(type_term)
                replaced = True
            continue
        out.append(tok)
    return _tidy(" ".join(out))


def sanitize_queries(queries: list[str], business_type: str, context: str | None = None,
                     type_term: str | None = None) -> list[str]:
    banned = banned_terms(business_type, context)
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        clean = sanitize_query(q, banned, type_term)
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            out.append(clean)
    return out


def strip_jurisdiction(query: str) -> str:
    """Fallback step 1: drop province/country terms (query was too narrow)."""
    lowered = query
    for phrase in _JURISDICTION_PHRASES:
        lowered = re.sub(rf"\b{re.escape(phrase)}\b", " ", lowered, flags=re.I)
    kept = [t for t in lowered.split() if t.strip(".,;:!?()\"'").lower() not in _JURISDICTIONS]
    return _tidy(" ".join(kept))


def broaden_vertical(query: str) -> str:
    """Fallback step 2: replace a niche vertical with its parent category."""
    for niche in sorted(_PARENT_VERTICAL, key=len, reverse=True):
        pattern = re.compile(rf"\b{re.escape(niche)}(e?s)?\b", re.I)
        match = pattern.search(query)
        if match:
            parent = _PARENT_VERTICAL[niche]
            if match.group(1) and not parent.endswith("s"):
                parent += "s"  # keep the plural the query had
            return _tidy(pattern.sub(parent, query, count=1))
    return query


def search_with_fallback(
    query: str,
    search_fn: Callable[..., list[dict[str, str]]],
    max_results: int = 5,
) -> tuple[list[dict[str, str]], str, str]:
    """Run the query; on 0 results retry broader before giving up.

    Returns (results, effective_query, stage) where stage is one of
    "original" | "jurisdiction_dropped" | "vertical_broadened" | "no_results".
    """
    tried = [("original", query)]
    without_jurisdiction = strip_jurisdiction(query)
    if without_jurisdiction and without_jurisdiction.lower() != query.lower():
        tried.append(("jurisdiction_dropped", without_jurisdiction))
    broadened = broaden_vertical(without_jurisdiction or query)
    if broadened.lower() not in {q.lower() for _, q in tried}:
        tried.append(("vertical_broadened", broadened))

    last_query = query
    errors = 0
    last_exc: Exception | None = None
    for stage, candidate in tried:
        try:
            results = search_fn(candidate, max_results=max_results)
        except Exception as exc:  # keep trying broader; report only if all fail
            errors += 1
            last_exc = exc
            results = []
        last_query = candidate
        if results:
            return results, candidate, stage
    if last_exc is not None and errors == len(tried):
        raise last_exc  # provider down, not a coverage gap — caller reports it
    return [], last_query, "no_results"

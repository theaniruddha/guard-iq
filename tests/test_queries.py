"""Synthesis query hygiene + zero-result fallback — fully offline (mocked search)."""

from gc_forge.synthesis.queries import (
    banned_terms,
    broaden_vertical,
    sanitize_queries,
    sanitize_query,
    search_with_fallback,
    strip_jurisdiction,
)

# The actual queries from the reported bug (business input: "Subway ... Brampton")
BUGGY_QUERIES = [
    "Subway Canada allergen information and labeling requirements",
    "Ontario public health regulations for fast food sandwich preparation and handling",
    "Subway Brampton customer complaints and incident reports 2023-2024",
    "Price promotion and discount advertising rules for quick-service restaurants in Ontario",
    "Employment standards and workplace safety for Subway franchise employees in Brampton",
    "Nutritional claim regulations for sandwich chains under the Canadian Food Inspection Agency",
]


def test_banned_terms_are_brand_and_city_only():
    banned = banned_terms("Subway", "sandwich shop in Brampton, Ontario area")
    assert "subway" in banned
    assert "brampton" in banned
    # generic business words never banned, even if the user capitalized them
    assert banned_terms("Pizza Restaurant", "Dental Clinic") == set()


def test_sanitized_queries_contain_no_brand_or_city():
    banned = banned_terms("Subway", "in Brampton")
    for q in BUGGY_QUERIES:
        clean = sanitize_query(q, banned, type_term="quick-service restaurant")
        low = clean.lower().split()
        assert "subway" not in low, clean
        assert "brampton" not in low, clean
        assert clean.strip(), "sanitizing must not empty the query"


def test_sanitize_replaces_brand_with_type_term():
    banned = banned_terms("Subway", None)
    clean = sanitize_query(
        "Subway Canada allergen information and labeling requirements",
        banned,
        type_term="quick-service restaurant",
    )
    assert clean.startswith("quick-service restaurant")
    assert "allergen" in clean  # risk dimension preserved


def test_sanitize_keeps_jurisdiction_on_first_pass():
    """Province/country stays in the query — regional law is signal, not noise."""
    banned = banned_terms("Subway", "Brampton")
    clean = sanitize_query(BUGGY_QUERIES[1], banned)
    assert "Ontario" in clean


def test_strip_jurisdiction_and_broaden_vertical():
    q = "Price promotion and discount advertising rules for quick-service restaurants in Ontario"
    no_juris = strip_jurisdiction(q)
    assert "ontario" not in no_juris.lower()
    assert "advertising rules" in no_juris  # dimension intact
    broader = broaden_vertical(no_juris)
    assert "quick-service restaurant" not in broader.lower()
    assert "restaurant" in broader.lower()  # parent category


def test_zero_results_triggers_broader_retry():
    calls: list[str] = []

    def fake_search(query: str, max_results: int = 5):
        calls.append(query)
        # only the jurisdiction-free version has coverage
        if "ontario" in query.lower():
            return []
        return [{"title": "hit", "url": "u", "snippet": "s"}]

    q = "Price promotion and discount advertising rules for quick-service restaurants in Ontario"
    results, effective, stage = search_with_fallback(q, fake_search)
    assert results, "fallback must rescue the dimension"
    assert stage == "jurisdiction_dropped"
    assert "ontario" not in effective.lower()
    assert calls[0] == q  # original tried first


def test_fallback_chains_to_vertical_broadening():
    def fake_search(query: str, max_results: int = 5):
        # nothing matches until the niche vertical is generalized
        if "sandwich chain" in query.lower():
            return []
        return [{"title": "hit", "url": "u", "snippet": "s"}]

    q = "Nutritional claim regulations for sandwich chains"
    results, effective, stage = search_with_fallback(q, fake_search)
    assert results
    assert stage == "vertical_broadened"
    assert "sandwich chain" not in effective.lower()


def test_all_stages_exhausted_reports_no_results():
    def dead_search(query: str, max_results: int = 5):
        return []

    results, _, stage = search_with_fallback("anything at all", dead_search)
    assert results == [] and stage == "no_results"


def test_sanitize_queries_dedupes():
    # two brand-variant queries that collapse to the same clean query
    cleaned = sanitize_queries(
        ["Subway allergen rules", "allergen rules"], "Subway", None
    )
    assert cleaned == ["allergen rules"]

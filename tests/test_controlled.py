"""Offline tests for the controlled-synthesis pipeline (no LLM calls)."""

from gc_forge.synthesis import controlled


def test_run_queries_uses_search_and_wraps_findings(monkeypatch):
    calls: list[str] = []

    def fake_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
        calls.append(query)
        return [{"title": f"t-{query}", "url": "http://x", "snippet": "s"}]

    monkeypatch.setattr(controlled.tools, "search", fake_search)

    findings = controlled.run_queries(["q1", "q2"])

    assert calls == ["q1", "q2"]
    assert [f.query for f in findings] == ["q1", "q2"]
    assert findings[0].results[0]["title"] == "t-q1"


def test_run_queries_survives_search_failure(monkeypatch):
    def broken_search(query: str, max_results: int = 5):
        raise RuntimeError("network down")

    monkeypatch.setattr(controlled.tools, "search", broken_search)

    findings = controlled.run_queries(["q1"])

    assert len(findings) == 1
    assert findings[0].results[0]["title"] == "search failed"
    assert "network down" in findings[0].results[0]["snippet"]


def test_findings_block_renders_queries_and_snippets():
    findings = [
        controlled.Finding(
            query="allergen law",
            results=[{"title": "FDA", "url": "http://fda.gov", "snippet": "labeling rules"}],
        ),
        controlled.Finding(query="empty", results=[]),
    ]
    block = controlled._findings_block(findings)
    assert "### Query: allergen law" in block
    assert "labeling rules" in block
    assert "(no results)" in block


def test_automatic_path_module_unchanged_by_import():
    # Importing controlled must not mutate the automatic agent module.
    from gc_forge.synthesis import agent

    assert agent.synthesize is not None
    assert agent.orchestrator_agent.output_type is not None

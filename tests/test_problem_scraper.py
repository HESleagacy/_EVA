from __future__ import annotations

from pathlib import Path

from pptx_forensics.problem_scraper import OfficialProblemScraper, parse_sih_problem_page


PAGE = """
<html><body>
<div id="ViewProblemStatement12345" class="modal">
  <table>
    <tr><th>Problem Statement ID</th><td><div>SIH12345</div></td></tr>
    <tr><th>Problem Statement Title</th><td>Reliable Local Navigation</td></tr>
    <tr><th>Description</th><td>
      Background: drivers lose service in tunnels.<br><br>
      Expected Solution<br>
      &#8226; Estimate position from local sensor data.<br>
      &#8226; Validate drift against a measurable benchmark.<br><br>
      Performance Benchmark: report results over a fixed route.
    </td></tr>
    <tr><th>Organization</th><td>Example Department</td></tr>
    <tr><th>Theme</th><td>Smart Mobility</td></tr>
  </table>
</div>
</body></html>
"""


def test_parse_sih_problem_page_extracts_official_fields_and_requirements() -> None:
    records = parse_sih_problem_page(PAGE)

    assert len(records) == 1
    assert records[0]["id"] == "12345"
    assert records[0]["title"] == "Reliable Local Navigation"
    assert records[0]["organization"] == "Example Department"
    assert [item["description"] for item in records[0]["requirements"]] == [
        "Estimate position from local sensor data.",
        "Validate drift against a measurable benchmark.",
        "Performance Benchmark: report results over a fixed route.",
    ]


def test_official_problem_scraper_reads_cache_without_a_second_fetch(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetcher(url: str, timeout: float) -> str:
        calls.append(url)
        return PAGE

    first = OfficialProblemScraper(
        "https://example.test/problems",
        cache_dir=tmp_path / "cache",
        fetcher=fetcher,
    ).resolve("PS-12345")
    second = OfficialProblemScraper(
        "https://example.test/problems",
        cache_dir=tmp_path / "cache",
        fetcher=lambda url, timeout: (_ for _ in ()).throw(AssertionError("cache was not used")),
    ).resolve("12345")

    assert first.to_dict() == second.to_dict()
    assert calls == ["https://example.test/problems"]

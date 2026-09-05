"""Deterministic retrieval of official problem statements from the SIH page."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .deck_evaluation import ProblemStatement, ProblemStatementError, validate_problem_statement


DEFAULT_SIH_PROBLEM_URL = "https://www.sih.gov.in/sih2026PS"
PROBLEM_CACHE_SCHEMA_VERSION = "official-problem-cache-1"
_MODAL_ID_PATTERN = re.compile(r"^ViewProblemStatement([A-Za-z0-9_-]+)$", re.IGNORECASE)
_EXPECTED_SOLUTION_PATTERN = re.compile(r"\bexpected\s+solution\b", re.IGNORECASE)
_BENCHMARK_PATTERN = re.compile(r"\bperformance\s+benchmark\b", re.IGNORECASE)


class ProblemScrapeError(ProblemStatementError):
    """Raised when an official problem statement cannot be fetched or parsed."""


def normalize_problem_id(value: Any) -> str:
    """Normalize official IDs such as ``SIH26168`` and ``PS-26168``."""
    text = str(value or "").strip()
    text = re.sub(r"^(?:sih|problem[-_ ]?statement|problem|ps)[-_ ]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").upper()
    return text or "UNASSIGNED"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


class _ProblemPageParser(HTMLParser):
    """Read problem records from the server-rendered detail modals."""

    _VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict[str, str]] = []
        self._active: dict[str, Any] | None = None
        self._depth = 0
        self._row: dict[str, list[str]] | None = None
        self._cell_kind: str | None = None
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._active is None:
            if tag.casefold() == "div":
                match = _MODAL_ID_PATTERN.fullmatch(str(attributes.get("id") or ""))
                if match:
                    self._active = {"modal_id": match.group(1), "fields": {}}
                    self._depth = 1
            return

        tag = tag.casefold()
        if tag == "br":
            if self._cell_kind is not None:
                self._cell_parts.append("\n")
            return
        if tag in self._VOID_TAGS:
            return
        if tag in {"li", "p"} and self._cell_kind is not None:
            self._cell_parts.append("\n")
        self._depth += 1
        if tag == "tr":
            self._finish_cell()
            self._finish_row()
            self._row = {"label": [], "value": []}
        elif tag in {"th", "td"} and self._row is not None:
            self._finish_cell()
            self._cell_kind = "label" if tag == "th" else "value"
            self._cell_parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br" and self._active is not None and self._cell_kind is not None:
            self._cell_parts.append("\n")
            return
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._active is None:
            return
        tag = tag.casefold()
        if tag in {"th", "td"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_cell()
            self._finish_row()
        if tag in self._VOID_TAGS:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._finish_active()

    def handle_data(self, data: str) -> None:
        if self._active is not None and self._cell_kind is not None:
            self._cell_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._active is not None:
            self._finish_active()

    def _finish_cell(self) -> None:
        if self._row is None or self._cell_kind is None:
            return
        self._row[self._cell_kind].extend(self._cell_parts)
        self._cell_kind = None
        self._cell_parts = []

    def _finish_row(self) -> None:
        if self._active is None or self._row is None:
            return
        label = _clean("".join(self._row["label"])).rstrip(":").casefold()
        value = _clean("".join(self._row["value"]))
        if label:
            self._active["fields"][label] = value
        self._row = None

    def _finish_active(self) -> None:
        if self._active is None:
            return
        self._finish_cell()
        self._finish_row()
        fields = self._active["fields"]
        record = {
            "id": _clean(fields.get("problem statement id") or self._active["modal_id"]),
            "title": _clean(fields.get("problem statement title")),
            "description": _clean(fields.get("description")),
            "organization": _clean(fields.get("organization")),
            "department": _clean(fields.get("department")),
            "category": _clean(fields.get("category")),
            "theme": _clean(fields.get("theme")),
        }
        if record["id"] and record["title"] and record["description"]:
            self.records.append(record)
        self._active = None
        self._depth = 0
        self._row = None
        self._cell_kind = None
        self._cell_parts = []


def parse_sih_problem_page(html: str) -> list[dict[str, Any]]:
    """Parse server-rendered official problem records without executing JavaScript."""
    parser = _ProblemPageParser()
    parser.feed(html)
    parser.close()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in parser.records:
        problem_id = normalize_problem_id(record["id"])
        if problem_id in seen:
            continue
        seen.add(problem_id)
        records.append(
            {
                **record,
                "id": problem_id,
                "requirements": _requirements(record["title"], record["description"]),
            }
        )
    return records


def _requirements(title: str, description: str) -> list[dict[str, Any]]:
    expected = _EXPECTED_SOLUTION_PATTERN.search(description)
    body = description[expected.end() :] if expected else description
    benchmark = _BENCHMARK_PATTERN.search(body)
    benchmark_text = ""
    if benchmark:
        benchmark_text = _clean(body[benchmark.end() :]).lstrip(": ")
        body = body[: benchmark.start()]

    chunks = [_clean(item).lstrip("-* ") for item in body.split("\u2022")]
    candidates = [item for item in chunks[1:] if len(item) >= 12] if len(chunks) > 1 else []
    if not candidates:
        candidates = [item for item in re.split(r"(?<=[.!?])\s+", body) if len(_clean(item)) >= 24]
    if not candidates:
        candidates = [f"Address the official problem statement: {title}."]
    if benchmark_text:
        candidates.append(f"Performance Benchmark: {benchmark_text}")

    unique: list[str] = []
    for candidate in candidates:
        cleaned = _clean(candidate)
        if cleaned and cleaned.casefold() not in {item.casefold() for item in unique}:
            unique.append(cleaned)
    return [
        {"id": f"r{index}", "description": item, "weight": 1.0}
        for index, item in enumerate(unique[:12], 1)
    ]


def _fetch_html(url: str, timeout: float) -> str:
    request = Request(url, headers={"Accept": "text/html", "User-Agent": "document-forensics-local/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read(25_000_000).decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ProblemScrapeError(f"could not fetch official problem statements from {url}: {exc}") from exc


class OfficialProblemScraper:
    """Fetch and cache official problem statements for deterministic local runs."""

    def __init__(
        self,
        url: str = DEFAULT_SIH_PROBLEM_URL,
        *,
        cache_dir: str | Path = ".cache/problem-statements",
        timeout: float = 20.0,
        refresh: bool = False,
        fetcher: Callable[[str, float], str] | None = None,
    ) -> None:
        parsed_scheme = re.match(r"^https?://", url, re.IGNORECASE)
        if not parsed_scheme:
            raise ValueError("official problem URL must use http or https")
        if timeout <= 0:
            raise ValueError("problem scraper timeout must be positive")
        self.url = url
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.timeout = timeout
        self.refresh = refresh
        self.fetcher = fetcher or _fetch_html
        self._problems: dict[str, ProblemStatement] | None = None
        self._load_error: ProblemScrapeError | None = None

    @property
    def cache_path(self) -> Path:
        digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def resolve(self, ps_id: str) -> ProblemStatement:
        """Resolve one normalized PS ID, fetching the official page once per run."""
        key = normalize_problem_id(ps_id)
        problems = self._load_problems()
        problem = problems.get(key)
        if problem is None:
            raise ProblemScrapeError(f"official problem statement {key} was not found at {self.url}")
        return problem

    def _load_problems(self) -> dict[str, ProblemStatement]:
        if self._problems is not None:
            return self._problems
        if self._load_error is not None:
            raise self._load_error
        if not self.refresh:
            cached = self._read_cache()
            if cached:
                self._problems = cached
                return cached
        try:
            html = self.fetcher(self.url, self.timeout)
        except ProblemScrapeError as exc:
            self._load_error = exc
            raise
        except Exception as exc:
            error = ProblemScrapeError(f"could not fetch official problem statements from {self.url}: {exc}")
            self._load_error = error
            raise error from exc
        records = parse_sih_problem_page(html)
        if not records:
            error = ProblemScrapeError(f"no problem statements were parsed from {self.url}")
            self._load_error = error
            raise error
        problems = {}
        for record in records:
            try:
                problem = validate_problem_statement(record)
            except ProblemStatementError:
                continue
            problems[normalize_problem_id(problem.id)] = problem
        if not problems:
            error = ProblemScrapeError(f"no valid problem statements were parsed from {self.url}")
            self._load_error = error
            raise error
        self._problems = problems
        self._write_cache(problems)
        return problems

    def _read_cache(self) -> dict[str, ProblemStatement] | None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping) or payload.get("schema_version") != PROBLEM_CACHE_SCHEMA_VERSION or payload.get("source_url") != self.url:
            return None
        raw_problems = payload.get("problems")
        if not isinstance(raw_problems, list):
            return None
        problems: dict[str, ProblemStatement] = {}
        for item in raw_problems:
            try:
                problem = validate_problem_statement(item)
            except ProblemStatementError:
                continue
            problems[normalize_problem_id(problem.id)] = problem
        return problems or None

    def _write_cache(self, problems: Mapping[str, ProblemStatement]) -> None:
        payload = {
            "schema_version": PROBLEM_CACHE_SCHEMA_VERSION,
            "source_url": self.url,
            "problems": [problems[key].to_dict() for key in sorted(problems)],
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            return

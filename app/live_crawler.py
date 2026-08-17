"""Live-mode crawl (bonus, see README §"Live investigation - built"). Given just a
domain, fetch a small set of public pages and turn them into the same Page shape
pass-in mode uses - so live mode is "live-fetched pages piped into the pass-in
code path", not a second implementation of feature inference to keep in sync
with the first (see main.py: both modes converge on the same `pages: list[Page]`
before anything LLM-related happens).

Deliberately thin, per the brief ("a couple of obvious surfaces" is explicitly
enough for the bonus):
  1. Fetch the homepage.
  2. Check robots.txt; drop any candidate path it disallows.
  3. Extract links from the homepage, rank them by how well they match known
     surface types (pricing, login, signup, docs), fetch the top few.
  4. Stop on whichever budget runs out first: maxPages or timeBudgetMs.
  5. Classify each fetched page's sourceType from its URL/content, strip tags to
     get readable text (good enough for the LLM step's evidence - not a real
     readability/boilerplate-removal pipeline; see "two more weeks").

No JS rendering, no auth, no pagination-following, no site-map parsing. That's
the deliberate scope line for "thin" - see README for what a real crawler would
add.
"""
from __future__ import annotations
import re
import time
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import httpx
from .models import Page

# Ranked by how likely a link matching this pattern is to carry useful evidence,
# and what sourceType it implies. Order matters: first match wins.
_SURFACE_PATTERNS: list[tuple[str, str]] = [
    (r"/pricing|/plans", "pricing"),
    (r"/login|/signin|/sign-in", "login"),
    (r"/signup|/sign-up|/register|/get-started", "signup"),
    (r"/docs|/documentation|/developer", "docs"),
    (r"/about|/product|/features", "marketing"),
]

_DEFAULT_SOURCE_TYPE = "marketing"


class _LinkAndTextExtractor(HTMLParser):
    """Minimal stdlib-only HTML handling: pull <a href> targets and visible text.
    Not a real readability pipeline (no boilerplate/nav stripping) - documented
    as a deliberate corner cut for a 'thin' crawler; see README."""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self._text_parts: list[str] = []
        self._skip_depth = 0  # inside <script>/<style>, don't collect text

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._text_parts.append(stripped)

    @property
    def text(self) -> str:
        return " ".join(self._text_parts)


def _classify_source_type(url: str) -> str:
    path = urlparse(url).path.lower()
    for pattern, source_type in _SURFACE_PATTERNS:
        if re.search(pattern, path):
            return source_type
    return _DEFAULT_SOURCE_TYPE


def _rank_candidate_links(base_url: str, hrefs: list[str]) -> list[str]:
    """Dedup, resolve to absolute URLs, keep same-origin only, then order by
    surface-pattern rank (matched patterns first, in _SURFACE_PATTERNS order),
    unmatched links last. This is the whole 'heuristic ranking' - deliberately
    simple, not a learned model; see README §5.5 for what a fuller version would
    look like (customer-reported break points, etc.)."""
    origin = urlparse(base_url)
    seen: set[str] = set()
    candidates: list[str] = []
    for href in hrefs:
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != origin.netloc:
            continue  # off-site link, not this site's evidence
        normalized = absolute.split("#")[0].rstrip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)

    def rank(url: str) -> int:
        path = urlparse(url).path.lower()
        for i, (pattern, _) in enumerate(_SURFACE_PATTERNS):
            if re.search(pattern, path):
                return i
        return len(_SURFACE_PATTERNS)

    return sorted(candidates, key=rank)


class LiveCrawlResult:
    def __init__(self, pages: list[Page], access_outcome: str, robots_blocked_count: int):
        self.pages = pages
        self.access_outcome = access_outcome
        self.robots_blocked_count = robots_blocked_count


async def crawl_site(
    domain: str,
    max_pages: int,
    time_budget_ms: int,
    client: httpx.AsyncClient | None = None,
) -> LiveCrawlResult:
    """Fetch up to `max_pages` pages from `domain`, stopping early if
    `time_budget_ms` elapses. Both are first-class stopping conditions checked
    before every fetch, not just an outer timeout wrapper (see README §5.5)."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    started_at = time.monotonic()
    base_url = f"https://{domain}"
    pages: list[Page] = []
    robots_blocked_count = 0

    def budget_exceeded() -> bool:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        return elapsed_ms >= time_budget_ms or len(pages) >= max_pages

    try:
        robots = urllib.robotparser.RobotFileParser()
        have_robots_rules = False
        try:
            robots_resp = await client.get(urljoin(base_url, "/robots.txt"))
            if robots_resp.status_code == 200:
                robots.parse(robots_resp.text.splitlines())
                have_robots_rules = True
            # any other status (404, 403, etc.) - no usable robots.txt, nothing to
            # disallow, fall through with have_robots_rules left False
        except httpx.HTTPError:
            pass  # unreachable robots.txt - fail open, don't block a whole crawl on it

        def allowed(url: str) -> bool:
            # NOTE: Python's RobotFileParser.can_fetch() defaults to DISALLOW when
            # no rules were ever successfully parsed into it (confirmed by testing,
            # not documented intuitively) - so it must only be consulted once we
            # know real rules were loaded, or a missing/unreadable robots.txt would
            # incorrectly block the entire crawl instead of correctly allowing it.
            if not have_robots_rules:
                return True
            try:
                return robots.can_fetch("*", url)
            except Exception:
                return True  # malformed robots.txt shouldn't block a well-behaved crawl

        # --- homepage ---
        if budget_exceeded():
            return LiveCrawlResult(pages, "blocked", robots_blocked_count)
        if not allowed(base_url):
            return LiveCrawlResult(pages, "blocked", robots_blocked_count + 1)

        try:
            resp = await client.get(base_url)
        except httpx.HTTPError:
            return LiveCrawlResult(pages, "blocked", robots_blocked_count)

        if resp.status_code == 401 or resp.status_code == 403:
            # gated even at the homepage - report it honestly rather than
            # pretending we have evidence (see BRIEF §4: a login wall on
            # authenticated content is itself weak evidence, not a failure)
            return LiveCrawlResult(pages, "blocked", robots_blocked_count)
        if resp.status_code >= 400:
            return LiveCrawlResult(pages, "blocked", robots_blocked_count)

        extractor = _LinkAndTextExtractor()
        extractor.feed(resp.text)
        pages.append(Page(
            url=base_url, sourceType="marketing", text=extractor.text[:4000],
            title=None, httpStatus=resp.status_code,
        ))

        # --- ranked follow-links ---
        candidates = _rank_candidate_links(base_url, extractor.links)
        homepage_normalized = base_url.rstrip("/")
        for candidate_url in candidates:
            if candidate_url.rstrip("/") == homepage_normalized:
                continue  # homepage linking to itself - already fetched, don't refetch
            if budget_exceeded():
                break
            if not allowed(candidate_url):
                robots_blocked_count += 1
                continue
            try:
                page_resp = await client.get(candidate_url)
            except httpx.HTTPError:
                continue  # one bad follow-link shouldn't abort the whole crawl
            if page_resp.status_code == 401 or page_resp.status_code == 403:
                continue  # this specific surface is gated; try the next candidate
            if page_resp.status_code >= 400:
                continue
            page_extractor = _LinkAndTextExtractor()
            page_extractor.feed(page_resp.text)
            pages.append(Page(
                url=candidate_url,
                sourceType=_classify_source_type(candidate_url),
                text=page_extractor.text[:4000],
                title=None,
                httpStatus=page_resp.status_code,
            ))

        access_outcome = "public-only" if pages else "blocked"
        if pages and len(pages) < max_pages and robots_blocked_count > 0:
            access_outcome = "partial"
        return LiveCrawlResult(pages, access_outcome, robots_blocked_count)
    finally:
        if owns_client:
            await client.aclose()

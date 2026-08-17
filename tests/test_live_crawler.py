"""Tests for app/live_crawler.py (bonus - see README "Live investigation -
built"). Uses a fake site served over httpx.ASGITransport, so these exercise
the real fetch/parse/rank/budget logic with zero real network calls."""
import time
import httpx
import pytest
from fastapi import FastAPI, Response
from app.live_crawler import crawl_site, _rank_candidate_links, _classify_source_type

HOMEPAGE_HTML = """
<html><body>
<h1>Acme Corp</h1>
<p>We build things.</p>
<a href="/pricing">Pricing</a>
<a href="/login">Log in</a>
<a href="/signup">Sign up</a>
<a href="/docs">Docs</a>
<a href="https://external-site.example/ad">External ad</a>
<a href="/about">About us</a>
</body></html>
"""

PRICING_HTML = "<html><body><h2>Plans</h2><p>Starter $10/mo, Pro $50/mo</p></body></html>"
LOGIN_HTML = "<html><body><form><input name='email'><input name='password'></form></body></html>"


def _fake_site_app(robots_txt: str | None = None, gated_paths: set[str] | None = None) -> FastAPI:
    gated_paths = gated_paths or set()
    app = FastAPI()

    @app.get("/robots.txt")
    async def robots():
        if robots_txt is None:
            return Response(status_code=404)
        return Response(content=robots_txt, media_type="text/plain")

    @app.get("/")
    async def home():
        if "/" in gated_paths:
            return Response(status_code=401)
        return Response(content=HOMEPAGE_HTML, media_type="text/html")

    @app.get("/pricing")
    async def pricing():
        if "/pricing" in gated_paths:
            return Response(status_code=401)
        return Response(content=PRICING_HTML, media_type="text/html")

    @app.get("/login")
    async def login():
        return Response(content=LOGIN_HTML, media_type="text/html")

    @app.get("/signup")
    async def signup():
        return Response(content="<html><body>Create your account</body></html>", media_type="text/html")

    @app.get("/docs")
    async def docs():
        return Response(content="<html><body>API docs</body></html>", media_type="text/html")

    @app.get("/about")
    async def about():
        return Response(content="<html><body>About Acme</body></html>", media_type="text/html")

    return app


async def _client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://acme-test.example")


# ---------- pure helpers ----------

def test_classify_source_type_from_path():
    assert _classify_source_type("https://x.test/pricing") == "pricing"
    assert _classify_source_type("https://x.test/login") == "login"
    assert _classify_source_type("https://x.test/signup") == "signup"
    assert _classify_source_type("https://x.test/docs/api") == "docs"
    assert _classify_source_type("https://x.test/random-page") == "marketing"


def test_rank_candidate_links_dedupes_and_drops_offsite():
    links = ["/pricing", "/pricing", "https://external.example/ad", "/login", "/random"]
    ranked = _rank_candidate_links("https://acme.test", links)
    assert "https://external.example/ad" not in ranked
    assert ranked.count("https://acme.test/pricing") == 1
    # known surfaces should rank ahead of the unmatched one
    assert ranked.index("https://acme.test/pricing") < ranked.index("https://acme.test/random")


def test_rank_candidate_links_includes_self_link_homepage_excludes_it_at_crawl_level():
    """A homepage self-link (e.g. a logo pointing at '/') is a valid candidate at
    the pure-ranking level - it's crawl_site's job to skip refetching whatever
    equals the homepage, not this function's. Documented here so the exclusion
    logic's home stays clear if this function ever changes."""
    ranked = _rank_candidate_links("https://acme.test", ["/", "https://acme.test/", "/pricing"])
    assert "https://acme.test" in ranked or "https://acme.test/" in ranked


# ---------- crawl_site integration ----------

@pytest.mark.asyncio
async def test_crawl_fetches_homepage_and_ranked_surfaces():
    app = _fake_site_app()
    async with await _client_for(app) as http_client:
        result = await crawl_site("acme-test.example", max_pages=5, time_budget_ms=5000, client=http_client)

    urls = [str(p.url) for p in result.pages]
    assert any(u.rstrip("/") == "https://acme-test.example" for u in urls)
    source_types = {p.sourceType for p in result.pages}
    assert "pricing" in source_types or "login" in source_types  # at least one ranked surface followed
    assert result.access_outcome == "public-only"


@pytest.mark.asyncio
async def test_crawl_stops_at_max_pages():
    app = _fake_site_app()
    async with await _client_for(app) as http_client:
        result = await crawl_site("acme-test.example", max_pages=2, time_budget_ms=5000, client=http_client)
    assert len(result.pages) <= 2


@pytest.mark.asyncio
async def test_crawl_respects_robots_disallow():
    robots_txt = "User-agent: *\nDisallow: /pricing\n"
    app = _fake_site_app(robots_txt=robots_txt)
    async with await _client_for(app) as http_client:
        result = await crawl_site("acme-test.example", max_pages=10, time_budget_ms=5000, client=http_client)
    urls = [str(p.url) for p in result.pages]
    assert not any("/pricing" in u for u in urls)
    assert result.robots_blocked_count >= 1


@pytest.mark.asyncio
async def test_crawl_reports_blocked_when_homepage_is_gated():
    app = _fake_site_app(gated_paths={"/"})
    async with await _client_for(app) as http_client:
        result = await crawl_site("acme-test.example", max_pages=5, time_budget_ms=5000, client=http_client)
    assert result.pages == []
    assert result.access_outcome == "blocked"


@pytest.mark.asyncio
async def test_crawl_continues_past_a_single_gated_surface():
    """Homepage is public but one follow-link (pricing) is gated - the crawl
    should skip that surface and still bring back the others, not abort
    entirely over one 401."""
    app = _fake_site_app(gated_paths={"/pricing"})
    async with await _client_for(app) as http_client:
        result = await crawl_site("acme-test.example", max_pages=10, time_budget_ms=5000, client=http_client)
    urls = [str(p.url) for p in result.pages]
    assert not any("/pricing" in u for u in urls)
    assert len(result.pages) >= 2  # homepage + at least one other surface
    assert result.access_outcome == "public-only"


@pytest.mark.asyncio
async def test_crawl_does_not_refetch_homepage_via_self_link():
    """Regression test: a homepage that links back to itself (e.g. a logo link to
    '/') should not produce a duplicate page - this was a real bug caught by
    running the crawler against a real site (pypi.org links to itself)."""
    app = FastAPI()

    @app.get("/robots.txt")
    async def robots():
        return Response(status_code=404)

    @app.get("/")
    async def home():
        html = '<html><body><a href="/">Home</a><a href="/pricing">Pricing</a></body></html>'
        return Response(content=html, media_type="text/html")

    @app.get("/pricing")
    async def pricing():
        return Response(content="<html><body>Plans</body></html>", media_type="text/html")

    async with await _client_for(app) as http_client:
        result = await crawl_site("acme-test.example", max_pages=10, time_budget_ms=5000, client=http_client)

    urls = [str(p.url).rstrip("/") for p in result.pages]
    assert urls.count("https://acme-test.example") == 1


@pytest.mark.asyncio
async def test_crawl_respects_time_budget():
    """A near-zero time budget should stop the crawl almost immediately -
    homepage might still get one fetch in, but it shouldn't chase every link."""
    app = _fake_site_app()
    async with await _client_for(app) as http_client:
        result = await crawl_site("acme-test.example", max_pages=100, time_budget_ms=0, client=http_client)
    assert len(result.pages) <= 1

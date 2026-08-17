"""Integration test for live mode through the actual POST /v1/evaluate endpoint -
not just crawl_site in isolation (that's tests/test_live_crawler.py). Monkeypatches
app.main.crawl_site so this tests the INTEGRATION logic in main.py (mode/
accessOutcome/trace wiring, pages flowing into the LLM step) rather than
re-testing the crawler's own HTTP behavior.
"""
import json
import os
import types
import httpx
import pytest
from httpx import ASGITransport
from fastapi import FastAPI, HTTPException
from app.main import app
from app import main as main_module
from app import catalog as catalog_module
from app.live_crawler import LiveCrawlResult
from app.models import Page
from .conftest import FIXTURES_DIR


async def _run_evaluate(payload: dict, fake_crawl_result: LiveCrawlResult) -> dict:
    with open(os.path.join(FIXTURES_DIR, "catalog.json"), encoding="utf-8") as fh:
        catalog_json = json.load(fh)
    catalog_app = FastAPI()

    @catalog_app.get("/v1/catalog")
    async def get_catalog(version: str | None = None):
        if version is not None and version != catalog_json["version"]:
            raise HTTPException(status_code=404)
        return catalog_json

    singleton = catalog_module.catalog_client
    singleton._by_version = {}
    singleton._latest_version = None
    singleton._latest_fetched_at = 0.0
    singleton.base_url = "http://catalog.test"
    catalog_transport = ASGITransport(app=catalog_app)

    async def fake_get(self, version, client=None):
        async with httpx.AsyncClient(transport=catalog_transport, base_url="http://catalog.test") as c:
            return await catalog_module.CatalogClient.get(self, version, client=c)

    singleton.get = types.MethodType(fake_get, singleton)

    async def fake_crawl_site(domain, max_pages, time_budget_ms, client=None):
        return fake_crawl_result

    original_crawl_site = main_module.crawl_site
    main_module.crawl_site = fake_crawl_site
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://evaluator.test") as client:
            resp = await client.post("/v1/evaluate", json=payload)
            assert resp.status_code == 200, resp.text
            return resp.json()
    finally:
        main_module.crawl_site = original_crawl_site


@pytest.mark.asyncio
async def test_live_mode_uses_crawled_pages_and_reports_trace():
    fake_pages = [
        Page(url="https://example-live.test", sourceType="marketing", text="Sign up for our SaaS product"),
        Page(url="https://example-live.test/login", sourceType="login", text="Email and password fields"),
    ]
    fake_result = LiveCrawlResult(pages=fake_pages, access_outcome="public-only", robots_blocked_count=1)

    payload = {
        "site": {"domain": "example-live.test"},
        "options": {"liveCrawl": {"enabled": True, "maxPages": 5, "timeBudgetMs": 5000}},
    }

    got = await _run_evaluate(payload, fake_result)

    assert got["investigation"]["mode"] == "live"
    assert got["investigation"]["accessOutcome"] == "public-only"
    assert got["investigation"]["pagesUsed"] == 2
    assert got["trace"]["liveCrawl"]["pagesFetched"] == 2
    assert got["trace"]["liveCrawl"]["robotsBlockedCount"] == 1
    assert got["trace"]["liveCrawl"]["maxPages"] == 5
    urls_used = {src["url"] for src in got["investigation"]["evidenceSources"]}
    assert "https://example-live.test/" in urls_used or "https://example-live.test" in urls_used


@pytest.mark.asyncio
async def test_live_mode_reports_blocked_when_crawl_finds_nothing():
    fake_result = LiveCrawlResult(pages=[], access_outcome="blocked", robots_blocked_count=0)
    payload = {
        "site": {"domain": "gated-site.test"},
        "options": {"liveCrawl": {"enabled": True}},
    }
    got = await _run_evaluate(payload, fake_result)
    assert got["investigation"]["mode"] == "live"
    assert got["investigation"]["accessOutcome"] == "blocked"
    assert got["investigation"]["pagesUsed"] == 0


@pytest.mark.asyncio
async def test_mixed_mode_falls_back_to_passed_in_pages_when_crawl_is_blocked():
    """content.pages given AND liveCrawl enabled = mixed mode. If the live crawl
    itself finds nothing, we should still be public-only off the passed-in pages,
    not blocked - the caller did give us usable evidence."""
    fake_result = LiveCrawlResult(pages=[], access_outcome="blocked", robots_blocked_count=0)
    payload = {
        "site": {"domain": "acme-hr.example"},
        "content": {"pages": [
            {"url": "https://acme-hr.example/login", "sourceType": "login", "text": "Email and password fields"}
        ]},
        "options": {"liveCrawl": {"enabled": True}},
    }
    got = await _run_evaluate(payload, fake_result)
    assert got["investigation"]["mode"] == "mixed"
    assert got["investigation"]["accessOutcome"] == "public-only"
    assert got["investigation"]["pagesUsed"] == 1

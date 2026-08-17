"""Tests for CatalogClient's retry/backoff resilience (bonus - see README).
Using fake httpx transport that can be scripted to fail N times before
succeeding, or fail permanently, so these run instantly with no real network
and no real sleeping (retry_base_delay is set to 0 in these tests).
"""
import json
import httpx
import pytest
from httpx import ASGITransport
from fastapi import FastAPI, HTTPException
from app.catalog import CatalogClient, CatalogUnavailableError, CatalogVersionNotFoundError

CATALOG_JSON = {"version": "v1", "featureClusters": [], "features": [], "capabilities": [], "archetypes": []}


def _flaky_catalog_app(fail_times: int, fail_status: int = 503):
    """A fake catalog server that returns `fail_status` for the first `fail_times`
    requests, then serves the real catalog."""
    state = {"calls": 0}
    app = FastAPI()

    @app.get("/v1/catalog")
    async def get_catalog(version: str | None = None):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise HTTPException(status_code=fail_status)
        if version is not None and version != CATALOG_JSON["version"]:
            raise HTTPException(status_code=404)
        return CATALOG_JSON

    return app, state


async def _client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://catalog.test")


@pytest.mark.asyncio
async def test_recovers_after_transient_failures_within_retry_budget():
    app, state = _flaky_catalog_app(fail_times=2)  # fails twice, succeeds on 3rd
    cc = CatalogClient(base_url="http://catalog.test", retry_attempts=3, retry_base_delay=0)
    async with await _client_for(app) as http_client:
        catalog = await cc.get(None, client=http_client)
    assert catalog.version == "v1"
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_pinned_version_raises_unavailable_after_exhausting_retries():
    app, state = _flaky_catalog_app(fail_times=99)  # always fails
    cc = CatalogClient(base_url="http://catalog.test", retry_attempts=3, retry_base_delay=0)
    async with await _client_for(app) as http_client:
        with pytest.raises(CatalogUnavailableError):
            await cc.get("v1", client=http_client)
    assert state["calls"] == 3  # exhausted all retries, not more, not fewer


@pytest.mark.asyncio
async def test_latest_falls_back_to_cached_version_after_exhausting_retries():
    app, state = _flaky_catalog_app(fail_times=0)
    cc = CatalogClient(base_url="http://catalog.test", retry_attempts=3, retry_base_delay=0, ttl_seconds=0)
    async with await _client_for(app) as http_client:
        first = await cc.get(None, client=http_client)  # populates the cache cleanly
    assert first.version == "v1"

    # now point the same client at a permanently-failing server and confirm it
    # falls back to the cached "latest" instead of raising
    bad_app, bad_state = _flaky_catalog_app(fail_times=99)
    async with await _client_for(bad_app) as http_client:
        second = await cc.get(None, client=http_client)
    assert second.version == "v1"
    assert bad_state["calls"] == 3  # still retried before falling back, not zero


@pytest.mark.asyncio
async def test_404_is_not_retried():
    """A confirmed 'version not found' is a real answer, not a glitch - retrying
    it would just waste time and delay the caller for no reason."""
    app, state = _flaky_catalog_app(fail_times=0)
    cc = CatalogClient(base_url="http://catalog.test", retry_attempts=5, retry_base_delay=0)
    async with await _client_for(app) as http_client:
        with pytest.raises(CatalogVersionNotFoundError):
            await cc.get("does-not-exist", client=http_client)
    assert state["calls"] == 1  # single attempt, no retries burned on a real 404

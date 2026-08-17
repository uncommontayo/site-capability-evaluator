"""Proves pass-in mode is reproducible: same request + same catalog version + same
evaluator version -> the same response, every time, with zero network calls (the LLM
step runs against the fixed cassette in tests/cassettes/, not a live API) - so this
test can't flake and doesn't burn API cost in CI. See README §5.3."""
import os
import httpx
import pytest
from httpx import ASGITransport
from app.main import app
from app import catalog as catalog_module
from app.config import settings
from .conftest import load_fixture, FIXTURES_DIR


async def _run_evaluate(payload: dict) -> dict:
    # Point the app's catalog client at the fake in-process catalog server instead
    # of a real network host, exactly the fixture data on disk.
    import json
    from fastapi import FastAPI, HTTPException

    with open(os.path.join(FIXTURES_DIR, "catalog.json"), encoding="utf-8") as fh:
        catalog_json = json.load(fh)
    catalog_app = FastAPI()

    @catalog_app.get("/v1/catalog")
    async def get_catalog(version: str | None = None):
        if version is not None and version != catalog_json["version"]:
            raise HTTPException(status_code=404)
        return catalog_json

    # main.py imported `catalog_client` by reference at module load time, so we must
    # mutate the SAME singleton instance rather than reassign catalog_module.catalog_client.
    singleton = catalog_module.catalog_client
    singleton._by_version = {}
    singleton._latest_version = None
    singleton._latest_fetched_at = 0.0
    singleton.base_url = "http://catalog.test"
    catalog_transport = ASGITransport(app=catalog_app)

    import types

    async def fake_get(self, version, client=None):
        async with httpx.AsyncClient(transport=catalog_transport, base_url="http://catalog.test") as c:
            return await catalog_module.CatalogClient.get(self, version, client=c)

    singleton.get = types.MethodType(fake_get, singleton)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://evaluator.test") as client:
        resp = await client.post("/v1/evaluate", json=payload)
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["acme-hr", "shopwave"])
async def test_pass_in_mode_is_deterministic_across_repeated_calls(name):
    payload, _ = load_fixture(name)
    first = await _run_evaluate(payload)
    second = await _run_evaluate(payload)
    third = await _run_evaluate(payload)
    assert first == second == third


@pytest.mark.asyncio
@pytest.mark.parametrize("name,expected_archetype", [("acme-hr", "saas-app"), ("shopwave", "ecommerce")])
async def test_pass_in_mode_reproduces_expected_rollup(name, expected_archetype):
    payload, expected = load_fixture(name)
    got = await _run_evaluate(payload)

    assert got["archetype"]["id"] == expected_archetype
    got_caps = {(c["capabilityId"], c["minLevel"], c["criticality"], tuple(sorted(c["sourceFeatureIds"])))
                for c in got["requiredCapabilities"]}
    expected_caps = {(c["capabilityId"], c["minLevel"], c["criticality"], tuple(sorted(c["sourceFeatureIds"])))
                     for c in expected["requiredCapabilities"]}
    assert got_caps == expected_caps
    assert got["investigation"]["mode"] == "passed-in"
    assert got["investigation"]["accessOutcome"] == "public-only"

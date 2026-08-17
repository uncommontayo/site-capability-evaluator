import json
import os
import pytest
import httpx
from fastapi import FastAPI, HTTPException

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _make_fake_catalog_app() -> FastAPI:
    """Stands in for the real Catalog API. Serves fixtures/catalog.json exactly as
    the spec describes: GET /v1/catalog[?version=...]. Used so CatalogClient's
    fetch+cache path is exercised for real (over ASGI, no live network) rather than
    special-cased in tests."""
    with open(os.path.join(FIXTURES_DIR, "catalog.json"), encoding="utf-8") as fh:
        catalog = json.load(fh)
    app = FastAPI()

    @app.get("/v1/catalog")
    async def get_catalog(version: str | None = None):
        if version is not None and version != catalog["version"]:
            raise HTTPException(status_code=404, detail="unknown catalog version")
        return catalog

    return app


@pytest.fixture
def fake_catalog_transport():
    app = _make_fake_catalog_app()
    return httpx.ASGITransport(app=app)


@pytest.fixture(autouse=True)
def isolate_catalog_cache():
    """Clears the singleton's cache state between tests. app/main.py imports the
    `catalog_client` object by reference at module load time, so reassigning
    catalog_module.catalog_client to a new instance would NOT reach main.py -
    we have to reset the existing instance in place instead."""
    from app import catalog as catalog_module
    singleton = catalog_module.catalog_client
    original_base_url = singleton.base_url
    original_get = singleton.get
    singleton._by_version = {}
    singleton._latest_version = None
    singleton._latest_fetched_at = 0.0
    yield
    singleton.base_url = original_base_url
    singleton.get = original_get
    singleton._by_version = {}
    singleton._latest_version = None
    singleton._latest_fetched_at = 0.0


def load_fixture(name: str) -> tuple[dict, dict]:
    # Fixtures are JSON (therefore UTF-8), and their page evidence includes em dashes.
    # On Windows the process default encoding can otherwise alter the evidence before
    # it reaches the recorded-cassette fingerprint.
    with open(os.path.join(FIXTURES_DIR, "sites", f"{name}.input.json"), encoding="utf-8") as fh:
        inp = json.load(fh)
    with open(os.path.join(FIXTURES_DIR, "sites", f"{name}.expected.json"), encoding="utf-8") as fh:
        exp = json.load(fh)
    return inp, exp

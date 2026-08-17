"""Fetches the Catalog from CATALOG_BASE and caches it in memory, keyed by version.

The catalog is described as versioned + immutable, so caching by version is safe
forever (no TTL needed for a *pinned* version). Kept a short TTL on the
"latest" pointer only, so a newly published version is picked up without a restart.
This is the only state this service holds (see README: stateless, in-memory cache
is the sole exception).

Resilience (bonus, see README §"Catalog resilience"): a transient failure (connection
error, timeout, 5xx) is retried with exponential backoff before giving up on it - a
404 is NOT retried, since that's a real, confirmed answer ("this version doesn't
exist"), not a glitch. After retries are exhausted, "latest" falls back to the
last-known-good version if we have one; a pinned version with no cached copy has
nothing to fall back to, so that surfaces as a 502 to the caller.
"""
from __future__ import annotations
import asyncio
import time
import httpx
from .config import settings
from .models import Catalog


class CatalogUnavailableError(Exception):
    pass


class CatalogVersionNotFoundError(Exception):
    pass


class _TransientFetchError(Exception):
    """Internal only: connection error, timeout, or 5xx after retries were exhausted.
    Distinct from 'not found' (a confirmed 404) so the two are never conflated."""


class CatalogClient:
    def __init__(
        self,
        base_url: str | None = None,
        ttl_seconds: int | None = None,
        retry_attempts: int | None = None,
        retry_base_delay: float | None = None,
    ):
        self.base_url = (base_url or settings.catalog_base).rstrip("/")
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else settings.catalog_cache_ttl_seconds
        self.retry_attempts = retry_attempts if retry_attempts is not None else settings.catalog_retry_attempts
        self.retry_base_delay = (
            retry_base_delay if retry_base_delay is not None else settings.catalog_retry_base_delay_seconds
        )
        self._by_version: dict[str, Catalog] = {}
        self._latest_version: str | None = None
        self._latest_fetched_at: float = 0.0

    async def get(self, version: str | None, client: httpx.AsyncClient | None = None) -> Catalog:
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=10.0)
        try:
            if version is not None:
                if version in self._by_version:
                    return self._by_version[version]
                try:
                    catalog = await self._fetch(client, version=version)
                except _TransientFetchError as exc:
                    raise CatalogUnavailableError(
                        f"could not reach the catalog service for version {version!r}: {exc}"
                    ) from exc
                if catalog is None:
                    raise CatalogVersionNotFoundError(version)
                self._by_version[catalog.version] = catalog
                return catalog

            # "latest" - re-check occasionally in case a new version was published
            now = time.monotonic()
            if self._latest_version and (now - self._latest_fetched_at) < self.ttl_seconds:
                return self._by_version[self._latest_version]

            try:
                catalog = await self._fetch(client, version=None)
            except _TransientFetchError:
                # exhausted retries - fall back to whatever we last called "latest",
                # same as a clean fetch failure would
                catalog = None

            if catalog is None:
                if self._latest_version:
                    return self._by_version[self._latest_version]
                raise CatalogUnavailableError("catalog endpoint unreachable and no cached copy available")

            self._by_version[catalog.version] = catalog
            self._latest_version = catalog.version
            self._latest_fetched_at = now
            return catalog
        finally:
            if owns_client:
                await client.aclose()

    async def _fetch(self, client: httpx.AsyncClient, version: str | None) -> Catalog | None:
        """One logical fetch, with retry+backoff around transient failures only.
        Returns None for a confirmed 404 (no retry - that's a real answer).
        Raises _TransientFetchError if every retry attempt failed."""
        url = f"{self.base_url}/v1/catalog"
        params = {"version": version} if version else None
        delay = self.retry_base_delay
        last_error: Exception | None = None

        for attempt in range(self.retry_attempts):
            try:
                resp = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if resp.status_code == 404:
                    return None  # confirmed, not a glitch - don't retry
                if resp.status_code >= 500:
                    last_error = RuntimeError(f"catalog service returned {resp.status_code}")
                elif resp.status_code >= 400:
                    return None  # other 4xx: treat as "not found" for this request, don't retry
                else:
                    return Catalog.model_validate(resp.json())

            if attempt < self.retry_attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2

        raise _TransientFetchError(str(last_error))


# Process-wide singleton so the cache is actually shared across requests.
catalog_client = CatalogClient()

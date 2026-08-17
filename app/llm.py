"""The fuzzy half of the service: "does this evidence show feature X is present?"

Design decisions (see README §5.1 / §5.2 for the full reasoning):

- The LLM ONLY answers per-feature presence/confidence/evidence questions. It never
  touches the capability rollup or does arithmetic - that's rollup.py, deterministic
  code, tested exactly against the fixtures.
- Batched by cluster, not one call per feature and not one mega-prompt. At production
  scale (~70 features / ~155 capabilities across maybe a dozen clusters) this keeps
  each prompt small enough to stay accurate and cheap, while cutting round-trips
  roughly 70-to-a-dozen versus one-call-per-feature. A single mega-prompt was rejected:
  it forces the model to hold the whole catalog's judgement calls in one pass, which is
  where accuracy degrades first as catalogs grow, and it makes partial-cluster caching
  impossible.
- temperature=0 and a fixed, versioned prompt template. That narrows variance a lot but
  does not make a live LLM call bit-for-bit deterministic across days/model updates -
  nothing genuinely does. So the *contractual* determinism guarantee (§5.3) is
  implemented one level up: this module is swappable behind LLMClient, and the
  determinism test in tests/test_determinism.py runs against RecordedLLMClient, a
  fixed-cassette stub with zero network calls and zero variance. In production this
  is the same pattern as VCR-style HTTP cassettes: CI proves the deterministic parts
  of the pipeline (rollup, response shaping) are stable; a separate, tolerant golden-set
  regression suite (comparing confidence/evidence within a tolerance band, exactly like
  the fixtures' own "_note") is what actually watches the LLM step for drift.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import time
from typing import Protocol
from .models import Feature, Page, InferredFeature

logger = logging.getLogger("app.llm")


def _log(event: str, **fields) -> None:
    """One JSON object per log line (structured, not free-text) so these are
    grep/parse-able in any log aggregator without a custom parser. Kept as a
    thin wrapper around stdlib logging rather than pulling in structlog - this
    is a take-home, not a production logging stack, but the *shape* of the
    output is the part that matters and it's the same shape either way."""
    logger.info(json.dumps({"event": event, **fields}, default=str))


class LLMClient(Protocol):
    async def infer_features(
        self, features: list[Feature], pages: list[Page]
    ) -> list[InferredFeature]: ...


def _pages_fingerprint(pages: list[Page]) -> str:
    # URL fields are Pydantic AnyUrl instances; JSON mode keeps the cassette
    # fingerprint based on their wire representation. AnyUrl canonicalizes a bare
    # origin with a trailing slash; retain the historic cassette convention where
    # that optional root slash is omitted.
    serialized_pages = [p.model_dump(mode="json") for p in pages]
    for page in serialized_pages:
        url = page["url"]
        if url.endswith("/") and url.count("/") == 3:
            page["url"] = url[:-1]
    payload = json.dumps(serialized_pages, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_cluster_prompt(features: list[Feature], pages: list[Page]) -> str:
    """The prompt template used for both the recorded cassettes and the real
    Anthropic client, so what's in the README is what actually ran."""
    evidence_block = "\n\n".join(
        f"[{p.sourceType}] {p.url}\ntitle: {p.title or ''}\n{(p.text or '')[:4000]}" for p in pages
    )
    questions_block = "\n".join(f"- {f.id}: {f.question}" for f in features)
    return f"""You are evaluating whether a web product has certain features, based only \
on the captured page evidence below. Answer ONLY from this evidence - do not guess \
about pages you have not seen, and do not use outside knowledge of the company.

For EACH feature listed, decide if it is present, not present, or there is not \
enough evidence either way (treat that as not present, with low confidence).

Features to evaluate:
{questions_block}

Captured page evidence:
{evidence_block}

Respond as a JSON array, one object per feature, in this exact shape:
[{{"featureId": "...", "present": true|false, "confidence": 0.0-1.0, \
"evidence": "one short line citing the specific page/text that supports this"}}]
"""


class RecordedLLMClient:
    """Deterministic, network-free stub used by tests and as the safe default so the
    service boots without an API key. Looks up a canned response by
    (feature-cluster, page-evidence fingerprint) from tests/cassettes/*.json.

    Falls back to a conservative "not present, low confidence" for anything that
    isn't in a cassette, rather than raising - so unrelated fixtures/clusters don't
    break just because they weren't recorded.
    """

    def __init__(self, cassette_dir: str):
        self.cassette_dir = cassette_dir
        self._cache: dict[str, dict] = {}

    def _load(self, name: str) -> dict:
        if name not in self._cache:
            import os
            path = os.path.join(self.cassette_dir, f"{name}.json")
            with open(path) as fh:
                self._cache[name] = json.load(fh)
        return self._cache[name]

    async def infer_features(self, features: list[Feature], pages: list[Page]) -> list[InferredFeature]:
        fp = _pages_fingerprint(pages)
        results: list[InferredFeature] = []
        by_fp = self._cassette_index()
        recorded = by_fp.get(fp, {})
        for f in features:
            if f.id in recorded:
                r = recorded[f.id]
                results.append(InferredFeature(featureId=f.id, present=r["present"], criticality="nice",
                                                confidence=r["confidence"], evidence=r["evidence"]))
            else:
                results.append(InferredFeature(featureId=f.id, present=False, criticality="nice",
                                                confidence=0.2, evidence="no supporting evidence in captured pages."))
        return results

    def _cassette_index(self) -> dict[str, dict]:
        # index all cassettes in the dir by their recorded fingerprint
        import os
        if getattr(self, "_index_cache", None) is not None:
            return self._index_cache
        index: dict[str, dict] = {}
        if os.path.isdir(self.cassette_dir):
            for fname in os.listdir(self.cassette_dir):
                if not fname.endswith(".json"):
                    continue
                data = self._load(fname[:-5])
                index[data["pagesFingerprint"]] = {row["featureId"]: row for row in data["features"]}
        self._index_cache = index
        return index


class LLMResponseValidationError(Exception):
    """Raised when a cluster's LLM response doesn't match the contract we asked
    for: missing a requested feature, inventing one that wasn't asked about,
    duplicating one, or failing to parse/shape-match at all. We treat model
    output as untrusted input - this is the boundary that enforces that (see
    README §5.1 and the 'LLM boundary' note)."""


def _validate_cluster_response(raw: object, expected_feature_ids: set[str]) -> list[dict]:
    """Structural validation only - field-level constraints (confidence in [0,1],
    criticality enum, etc.) are still enforced by Pydantic when InferredFeature is
    constructed from each row afterwards. This function's job is narrower: did the
    model answer the question we actually asked, for exactly the features we asked
    about, in the shape we asked for."""
    if not isinstance(raw, list):
        raise LLMResponseValidationError(f"expected a JSON array, got {type(raw).__name__}")

    seen_ids: set[str] = set()
    rows: list[dict] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise LLMResponseValidationError(f"item {i} is not an object: {row!r}")
        missing_keys = {"featureId", "present", "confidence", "evidence"} - row.keys()
        if missing_keys:
            raise LLMResponseValidationError(f"item {i} missing required keys: {sorted(missing_keys)}")

        fid = row["featureId"]
        if fid not in expected_feature_ids:
            raise LLMResponseValidationError(f"unknown featureId {fid!r} was not in the requested cluster")
        if fid in seen_ids:
            raise LLMResponseValidationError(f"duplicate featureId {fid!r} in response")
        seen_ids.add(fid)
        rows.append(row)

    missing_features = expected_feature_ids - seen_ids
    if missing_features:
        raise LLMResponseValidationError(f"response is missing features: {sorted(missing_features)}")

    return rows



class AnthropicLLMClient:
    """Real implementation, batched by feature cluster. Requires anthropic SDK + key.
    Not used in CI (see RecordedLLMClient above) - wired up for production use.

    Treats the model's response as untrusted input at the cluster boundary:
    validate shape (see _validate_cluster_response), retry once on failure, and
    fall back to a conservative "not present, low confidence" for that cluster
    if the retry also fails - rather than letting a malformed response either
    crash the whole evaluation or silently propagate bad data. Every outcome is
    recorded on `metrics` so it's possible to see how often this boundary is
    actually doing something (see app/metrics.py).

    Cluster calls run concurrently, bounded by a semaphore (see README
    "Concurrency, logging & cost accounting"): clusters are independent of each
    other, so there's no correctness reason to run them one at a time, but
    unbounded concurrency on a ~12-cluster production catalog means every single
    evaluation fires a burst of simultaneous requests at the LLM provider - fine
    at toy scale, a fast way to trip rate limits at real scale. Every call also
    logs a structured event and records its token usage, whether or not the
    response ultimately validates."""

    def __init__(
        self,
        api_key: str,
        model: str,
        metrics=None,
        max_concurrent_clusters: int | None = None,
    ):
        self.model = model
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        from .metrics import guardrail_metrics
        from .config import settings as _settings
        self.metrics = metrics if metrics is not None else guardrail_metrics
        limit = max_concurrent_clusters if max_concurrent_clusters is not None else _settings.llm_max_concurrent_clusters
        self._semaphore = asyncio.Semaphore(limit)

    async def _call_cluster(self, cluster_id: str, cluster_features: list[Feature], pages: list[Page]) -> list[dict]:
        """One attempt at one cluster: send the prompt, record token usage
        regardless of outcome, then validate. Raises on any failure - caller
        decides whether to retry or fall back."""
        prompt = build_cluster_prompt(cluster_features, pages)
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=1500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )

        usage = getattr(resp, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        self.metrics.record_tokens(input_tokens, output_tokens)

        text = "".join(block.text for block in resp.content if hasattr(block, "text"))
        raw = json.loads(text)
        expected_ids = {f.id for f in cluster_features}
        return _validate_cluster_response(raw, expected_ids)

    async def _infer_cluster(
        self, cluster_id: str, cluster_features: list[Feature], pages: list[Page]
    ) -> list[InferredFeature]:
        async with self._semaphore:
            self.metrics.record_cluster_call()
            _log("cluster_call_start", cluster_id=cluster_id, feature_count=len(cluster_features))
            started_at = time.monotonic()
            rows = None

            for attempt in range(2):  # first attempt + one retry
                try:
                    rows = await self._call_cluster(cluster_id, cluster_features, pages)
                    if attempt == 1:
                        self.metrics.record_retry_success()
                        _log("cluster_retry_succeeded", cluster_id=cluster_id)
                    break
                except (LLMResponseValidationError, json.JSONDecodeError, KeyError, TypeError) as exc:
                    self.metrics.record_validation_failure()
                    _log("cluster_validation_failed", cluster_id=cluster_id, attempt=attempt, error=str(exc))
                    continue

            latency_ms = round((time.monotonic() - started_at) * 1000, 1)

            if rows is None:
                # Both attempts failed validation. Fall back to a conservative
                # default rather than crashing the whole evaluation over one
                # bad cluster response - same principle as a gated site: report
                # low confidence honestly instead of failing outright.
                self.metrics.record_fallback_used()
                _log("cluster_fallback_used", cluster_id=cluster_id, latency_ms=latency_ms)
                return [
                    InferredFeature(
                        featureId=f.id, present=False, criticality="nice",
                        confidence=0.0, evidence="LLM response failed validation twice; defaulted to not present.",
                    )
                    for f in cluster_features
                ]

            _log("cluster_call_complete", cluster_id=cluster_id, latency_ms=latency_ms, feature_count=len(rows))
            results = []
            for row in rows:
                confidence = float(row["confidence"])
                self.metrics.record_confidence(confidence)
                results.append(InferredFeature(
                    featureId=row["featureId"], present=row["present"], criticality="nice",
                    confidence=confidence, evidence=row["evidence"],
                ))
            return results

    async def infer_features(self, features: list[Feature], pages: list[Page]) -> list[InferredFeature]:
        clusters: dict[str, list[Feature]] = {}
        for f in features:
            clusters.setdefault(f.clusterId, []).append(f)

        # Concurrent, bounded by self._semaphore inside _infer_cluster. asyncio.gather
        # preserves task order in its results regardless of completion order, so the
        # final flattened list doesn't depend on which cluster happens to finish first.
        per_cluster_results = await asyncio.gather(
            *(
                self._infer_cluster(cluster_id, cluster_features, pages)
                for cluster_id, cluster_features in clusters.items()
            )
        )
        return [feature for cluster_result in per_cluster_results for feature in cluster_result]


def get_llm_client(settings) -> LLMClient:
    if settings.llm_client == "anthropic":
        return AnthropicLLMClient(api_key=settings.llm_api_key, model=settings.llm_model)
    import os
    default_dir = os.path.join(os.path.dirname(__file__), "..", "tests", "cassettes")
    return RecordedLLMClient(cassette_dir=default_dir)

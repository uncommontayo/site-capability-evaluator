"""Tests for the LLM response validation layer and guardrail metrics
(app/llm.py's AnthropicLLMClient + app/metrics.py). No real Anthropic calls -
Swap a fake `messages.create` so these run offline like everything else.
"""
import json
import pytest
from app.llm import AnthropicLLMClient, LLMResponseValidationError, _validate_cluster_response
from app.metrics import GuardrailMetrics
from app.models import Feature, CapabilityRequirement, Page


def _feature(fid: str, cluster: str = "auth") -> Feature:
    return Feature(id=fid, name=fid, question=f"does it have {fid}?", clusterId=cluster, requires=[])


def _page() -> Page:
    return Page(url="https://example.test", sourceType="marketing", text="hello world")


class _FakeMessages:
    """Stands in for client.messages - returns canned text responses in sequence,
    one per call, so a test can script "first call malformed, second call clean"."""
    def __init__(self, response_texts: list[str]):
        self._texts = list(response_texts)
        self.call_count = 0

    async def create(self, **kwargs):
        text = self._texts[self.call_count]
        self.call_count += 1
        block = type("Block", (), {"text": text})()
        return type("Resp", (), {"content": [block]})()


def _client_with_responses(response_texts: list[str], metrics: GuardrailMetrics) -> AnthropicLLMClient:
    import asyncio
    client = AnthropicLLMClient.__new__(AnthropicLLMClient)  # skip __init__'s real SDK setup
    client.model = "test-model"
    client._client = type("C", (), {"messages": _FakeMessages(response_texts)})()
    client.metrics = metrics
    client._semaphore = asyncio.Semaphore(4)
    return client


# ---------- _validate_cluster_response (pure, structural) ----------

def test_validate_accepts_exact_match():
    raw = [{"featureId": "a", "present": True, "confidence": 0.9, "evidence": "x"}]
    rows = _validate_cluster_response(raw, {"a"})
    assert rows == raw


def test_validate_rejects_missing_feature():
    raw = [{"featureId": "a", "present": True, "confidence": 0.9, "evidence": "x"}]
    with pytest.raises(LLMResponseValidationError, match="missing features"):
        _validate_cluster_response(raw, {"a", "b"})


def test_validate_rejects_invented_feature():
    raw = [{"featureId": "z", "present": True, "confidence": 0.9, "evidence": "x"}]
    with pytest.raises(LLMResponseValidationError, match="unknown featureId"):
        _validate_cluster_response(raw, {"a"})


def test_validate_rejects_duplicate_feature():
    raw = [
        {"featureId": "a", "present": True, "confidence": 0.9, "evidence": "x"},
        {"featureId": "a", "present": False, "confidence": 0.1, "evidence": "y"},
    ]
    with pytest.raises(LLMResponseValidationError, match="duplicate"):
        _validate_cluster_response(raw, {"a"})


def test_validate_rejects_non_list():
    with pytest.raises(LLMResponseValidationError, match="expected a JSON array"):
        _validate_cluster_response({"featureId": "a"}, {"a"})


def test_validate_rejects_missing_keys():
    raw = [{"featureId": "a", "present": True}]  # no confidence/evidence
    with pytest.raises(LLMResponseValidationError, match="missing required keys"):
        _validate_cluster_response(raw, {"a"})


# ---------- AnthropicLLMClient end-to-end with the fake transport ----------

@pytest.mark.asyncio
async def test_clean_response_records_confidence_and_no_failures():
    metrics = GuardrailMetrics()
    good = json.dumps([{"featureId": "a", "present": True, "confidence": 0.8, "evidence": "seen"}])
    client = _client_with_responses([good], metrics)

    result = await client.infer_features([_feature("a")], [_page()])

    assert len(result) == 1
    assert result[0].confidence == 0.8
    assert metrics.cluster_calls == 1
    assert metrics.validation_failures == 0
    assert metrics.fallback_used == 0
    assert metrics.confidences_seen == [0.8]


@pytest.mark.asyncio
async def test_malformed_then_clean_response_retries_and_recovers():
    metrics = GuardrailMetrics()
    bad = "not valid json at all"
    good = json.dumps([{"featureId": "a", "present": True, "confidence": 0.7, "evidence": "seen"}])
    client = _client_with_responses([bad, good], metrics)

    result = await client.infer_features([_feature("a")], [_page()])

    assert len(result) == 1
    assert result[0].present is True
    assert metrics.validation_failures == 1
    assert metrics.retry_successes == 1
    assert metrics.fallback_used == 0


@pytest.mark.asyncio
async def test_malformed_twice_falls_back_conservatively():
    metrics = GuardrailMetrics()
    bad = "still not json"
    client = _client_with_responses([bad, bad], metrics)

    result = await client.infer_features([_feature("a")], [_page()])

    assert len(result) == 1
    assert result[0].present is False
    assert result[0].confidence == 0.0
    assert metrics.validation_failures == 2
    assert metrics.fallback_used == 1
    # fallback path must not silently claim confidence in bad data
    assert metrics.confidences_seen == []


@pytest.mark.asyncio
async def test_invented_feature_id_triggers_validation_failure_path():
    metrics = GuardrailMetrics()
    wrong_id = json.dumps([{"featureId": "does-not-exist", "present": True, "confidence": 0.9, "evidence": "x"}])
    good = json.dumps([{"featureId": "a", "present": True, "confidence": 0.6, "evidence": "y"}])
    client = _client_with_responses([wrong_id, good], metrics)

    result = await client.infer_features([_feature("a")], [_page()])

    assert result[0].featureId == "a"
    assert metrics.validation_failures == 1
    assert metrics.retry_successes == 1


def test_metrics_snapshot_and_rate_math():
    m = GuardrailMetrics()
    m.record_cluster_call()
    m.record_cluster_call()
    m.record_validation_failure()
    m.record_confidence(0.5)
    m.record_confidence(0.9)

    snap = m.snapshot()
    assert snap["cluster_calls"] == 2
    assert snap["validation_failure_rate"] == 0.5
    assert snap["mean_confidence"] == 0.7


# ---------- token accounting ----------

def test_tokens_recorded_regardless_of_validation_outcome():
    """Tokens were spent whether or not the response later validates - cost
    accounting has to reflect that, not just count the calls that "worked"."""
    m = GuardrailMetrics()
    m.record_tokens(100, 40)
    m.record_tokens(50, 10)
    assert m.total_input_tokens == 150
    assert m.total_output_tokens == 50


def test_estimated_cost_uses_configured_rate_not_a_hardcoded_one():
    m = GuardrailMetrics()
    m.record_tokens(1_000_000, 1_000_000)
    # zero rate (the default) means zero cost - no silently-assumed pricing
    assert m.estimated_cost_usd(0.0, 0.0) == 0.0
    # a configured rate is applied straightforwardly: $3/M in, $15/M out
    assert m.estimated_cost_usd(3.0, 15.0) == pytest.approx(18.0)


@pytest.mark.asyncio
async def test_real_call_records_token_usage_from_response():
    metrics = GuardrailMetrics()
    good = json.dumps([{"featureId": "a", "present": True, "confidence": 0.8, "evidence": "seen"}])
    client = _client_with_responses([good], metrics)
    # patch the fake transport's response to carry a usage object, like the real SDK does
    usage = type("Usage", (), {"input_tokens": 250, "output_tokens": 40})()
    original_create = client._client.messages.create

    async def create_with_usage(**kwargs):
        resp = await original_create(**kwargs)
        resp.usage = usage
        return resp

    client._client.messages.create = create_with_usage

    await client.infer_features([_feature("a")], [_page()])

    assert metrics.total_input_tokens == 250
    assert metrics.total_output_tokens == 40


# ---------- concurrency bounding ----------

@pytest.mark.asyncio
async def test_cluster_calls_are_bounded_by_semaphore():
    """Three clusters, concurrency capped at 1 - if the semaphore is working,
    at most one cluster's LLM call should be in flight at any instant.

    Uses three separate client instances (each with its own fake transport)
    sharing one semaphore, rather than mutating one client's transport from
    concurrent tasks - that would race, since the tasks interleave around the
    semaphore's suspend points."""
    import asyncio

    max_concurrent_observed = 0
    in_flight = 0
    shared_semaphore = asyncio.Semaphore(1)
    shared_metrics = GuardrailMetrics()

    def make_client(fid: str) -> AnthropicLLMClient:
        async def create(**kwargs):
            nonlocal max_concurrent_observed, in_flight
            in_flight += 1
            max_concurrent_observed = max(max_concurrent_observed, in_flight)
            await asyncio.sleep(0.01)  # simulate network latency
            in_flight -= 1
            payload = json.dumps([{"featureId": fid, "present": True, "confidence": 0.5, "evidence": "e"}])
            block = type("Block", (), {"text": payload})()
            return type("Resp", (), {"content": [block]})()

        c = AnthropicLLMClient.__new__(AnthropicLLMClient)
        c.model = "test-model"
        c._client = type("C", (), {"messages": type("M", (), {"create": staticmethod(create)})()})()
        c.metrics = shared_metrics
        c._semaphore = shared_semaphore
        return c

    await asyncio.gather(
        make_client("a")._infer_cluster("c1", [_feature("a", cluster="c1")], [_page()]),
        make_client("b")._infer_cluster("c2", [_feature("b", cluster="c2")], [_page()]),
        make_client("c")._infer_cluster("c3", [_feature("c", cluster="c3")], [_page()]),
    )
    assert max_concurrent_observed == 1


@pytest.mark.asyncio
async def test_cluster_calls_do_run_concurrently_when_limit_allows():
    """Sanity check the other direction: with a limit of 3 (>= cluster count),
    all three should be in flight at once, not accidentally serialized."""
    import asyncio

    max_concurrent_observed = 0
    in_flight = 0
    shared_semaphore = asyncio.Semaphore(3)
    shared_metrics = GuardrailMetrics()

    def make_client(fid: str) -> AnthropicLLMClient:
        async def create(**kwargs):
            nonlocal max_concurrent_observed, in_flight
            in_flight += 1
            max_concurrent_observed = max(max_concurrent_observed, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            payload = json.dumps([{"featureId": fid, "present": True, "confidence": 0.5, "evidence": "e"}])
            block = type("Block", (), {"text": payload})()
            return type("Resp", (), {"content": [block]})()

        c = AnthropicLLMClient.__new__(AnthropicLLMClient)
        c.model = "test-model"
        c._client = type("C", (), {"messages": type("M", (), {"create": staticmethod(create)})()})()
        c.metrics = shared_metrics
        c._semaphore = shared_semaphore
        return c

    await asyncio.gather(
        make_client("a")._infer_cluster("c1", [_feature("a", cluster="c1")], [_page()]),
        make_client("b")._infer_cluster("c2", [_feature("b", cluster="c2")], [_page()]),
        make_client("c")._infer_cluster("c3", [_feature("c", cluster="c3")], [_page()]),
    )
    assert max_concurrent_observed == 3

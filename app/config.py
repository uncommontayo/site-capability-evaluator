"""All configuration comes from the environment."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    catalog_base: str = os.environ.get("CATALOG_BASE", "http://localhost:8000/mock-catalog")
    llm_provider: str = os.environ.get("LLM_PROVIDER", "anthropic")
    llm_model: str = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    llm_api_key: str = os.environ.get("LLM_API_KEY", "")
    # Selects which LLM client implementation to use. "recorded" replays fixed
    # cassette responses (used by CI / the determinism test, and as a safe default
    # so the service boots without a key). "anthropic" makes real calls.
    llm_client: str = os.environ.get("LLM_CLIENT", "recorded")
    evaluator_version: str = os.environ.get("EVALUATOR_VERSION", "0.1.0")
    catalog_cache_ttl_seconds: int = int(os.environ.get("CATALOG_CACHE_TTL_SECONDS", "300"))
    catalog_retry_attempts: int = int(os.environ.get("CATALOG_RETRY_ATTEMPTS", "3"))
    catalog_retry_base_delay_seconds: float = float(os.environ.get("CATALOG_RETRY_BASE_DELAY_SECONDS", "0.2"))
    # Bounds how many feature-cluster LLM calls run concurrently per /v1/evaluate
    # request. Cluster calls are independent (each only needs the page evidence,
    # not another cluster's result), so they're safe to parallelize - but
    # unbounded concurrency on a 12-cluster production catalog would fire 12
    # simultaneous requests at the LLM provider per evaluation, which is a good
    # way to hit rate limits under any real load. Bounded, not sequential and not
    # unbounded.
    llm_max_concurrent_clusters: int = int(os.environ.get("LLM_MAX_CONCURRENT_CLUSTERS", "4"))
    # Cost accounting is opt-in: 0.0 means "don't estimate cost, just report raw
    # token counts". We deliberately do not hard-code a $/token figure as a
    # non-zero default - model pricing changes over time and a stale hard-coded
    # number would be actively misleading. Set these from your provider's current
    # published rate if you want a cost estimate.
    llm_input_cost_per_million_tokens: float = float(os.environ.get("LLM_INPUT_COST_PER_MILLION_TOKENS", "0.0"))
    llm_output_cost_per_million_tokens: float = float(os.environ.get("LLM_OUTPUT_COST_PER_MILLION_TOKENS", "0.0"))
    default_live_max_pages: int = int(os.environ.get("DEFAULT_LIVE_MAX_PAGES", "8"))
    default_live_time_budget_ms: int = int(os.environ.get("DEFAULT_LIVE_TIME_BUDGET_MS", "20000"))


settings = Settings()

"""Guardrail metrics for the LLM boundary (see README §5.1 / llm.py's validation
layer) plus token/cost accounting (bonus - see README "Concurrency, logging &
cost accounting"). Deliberately minimal: in-memory counters, no external
metrics backend, no time-series storage. The point isn't a dashboard, it's
answering two questions after the fact: "how often is validation actually
catching something" and "what is this evaluation actually costing" - both of
which you'd want to know before trusting an LLM-backed step in production (see
README "two more weeks": golden-set regression / drift monitoring).

Not wired into any exporter (Prometheus, StatsD, etc.) - that's a real next
step, not this one. This module is the seam where that would plug in: swap
GuardrailMetrics.record_* for calls into whatever backend, without touching
llm.py.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class GuardrailMetrics:
    cluster_calls: int = 0
    validation_failures: int = 0
    retry_successes: int = 0
    fallback_used: int = 0
    confidences_seen: list[float] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def record_cluster_call(self) -> None:
        self.cluster_calls += 1

    def record_validation_failure(self) -> None:
        self.validation_failures += 1

    def record_retry_success(self) -> None:
        self.retry_successes += 1

    def record_fallback_used(self) -> None:
        self.fallback_used += 1

    def record_confidence(self, value: float) -> None:
        self.confidences_seen.append(value)

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """Recorded per API call, regardless of whether that call's response later
        passed validation - tokens were spent either way, so cost accounting must
        reflect that rather than only counting the calls that "worked"."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

    @property
    def validation_failure_rate(self) -> float:
        return self.validation_failures / self.cluster_calls if self.cluster_calls else 0.0

    @property
    def mean_confidence(self) -> float:
        return sum(self.confidences_seen) / len(self.confidences_seen) if self.confidences_seen else 0.0

    def estimated_cost_usd(self, input_cost_per_million: float, output_cost_per_million: float) -> float:
        return (
            self.total_input_tokens / 1_000_000 * input_cost_per_million
            + self.total_output_tokens / 1_000_000 * output_cost_per_million
        )

    def snapshot(self, input_cost_per_million: float = 0.0, output_cost_per_million: float = 0.0) -> dict:
        return {
            "cluster_calls": self.cluster_calls,
            "validation_failures": self.validation_failures,
            "validation_failure_rate": round(self.validation_failure_rate, 4),
            "retry_successes": self.retry_successes,
            "fallback_used": self.fallback_used,
            "mean_confidence": round(self.mean_confidence, 4),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "estimated_cost_usd": round(
                self.estimated_cost_usd(input_cost_per_million, output_cost_per_million), 6
            ),
        }


# Process-wide instance. Good enough for a single-process service; a real
# deployment would push these into a shared backend instead (see module docstring).
guardrail_metrics = GuardrailMetrics()

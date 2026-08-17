# Site Capability Evaluator

Original brief is in [`BRIEF.md`](./BRIEF.md). This file covers how to run it, what I built vs what I skipped, and my reasoning on the six decisions from BRIEF §5.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# terminal 1: stand-in Catalog API (serves fixtures/catalog.json)
python scripts/serve_catalog.py

# terminal 2: the evaluator itself
export CATALOG_BASE=http://localhost:8000
export LLM_CLIENT=recorded   # no API key needed, see "Determinism" below
uvicorn app.main:app --port 8080

curl -s -X POST localhost:8080/v1/evaluate \
  -H "Content-Type: application/json" \
  -d @fixtures/sites/acme-hr.input.json | python3 -m json.tool
```

Or with Docker, same defaults, boots without a key:

```bash
docker build -t evaluator .
docker run -p 8080:8080 -e CATALOG_BASE=http://host.docker.internal:8000 evaluator
```

`pytest -q` runs everything, no network, under a second.

## The LLM prompt

One prompt template, reused per feature-cluster batch, see `app/llm.py::build_cluster_prompt`. It only gets the captured page evidence and the natural-language `question` for each feature in that cluster, and is told to answer only from that evidence, in a fixed JSON shape. It never sees the requires/capability graph, that's not its job, more on this below.

## §5 decisions

### 1. Where fuzzy judgment ends and deterministic code starts

The LLM answers exactly one kind of question: does this evidence show feature X is present, and how confident am I. That's genuine judgment, "does this page's copy describe a multi-step form" isn't something you can regex reliably. Everything after that, which capabilities a present feature requires, how levels and criticalities roll up when multiple features share a capability, which archetype fits best, is plain code in `app/rollup.py`. No LLM involvement at all. I worked out the roll-up rule from the two fixtures (see the module docstring), and it reproduces both exactly, including the ShopWave case where a capability's level and its criticality trace back to different source features.

One thing I deliberately kept out of the LLM's hands even though it could technically do it: picking the archetype. It's a small, well-defined optimization over inputs that are already fuzzy, so running it as plain code means the archetype pick isn't adding its own variance on top of the feature-level fuzziness. It's also free, no extra API call.

The LLM boundary is treated as untrusted input, not just untrusted judgment. `AnthropicLLMClient` in `app/llm.py` never builds an `InferredFeature` straight off a parsed model response. Each cluster's response goes through `_validate_cluster_response` first, does it parse as a JSON array, does every object have the required keys, does the set of returned `featureId`s match exactly what was asked for (no missing, no invented, no duplicates). Fails any of that, it gets one retry. Fails again, that cluster falls back to "not present, confidence 0" instead of crashing the whole evaluation or quietly trusting bad data. Every outcome (call, failure, retry recovery, fallback) gets counted in `GuardrailMetrics` and exposed read-only at `GET /internal/guardrail-metrics`, so the failure rate of this boundary is something you can actually look at rather than assume. `RecordedLLMClient`, which every test runs against, doesn't touch this path at all, it's cassette data, nothing to validate. The counters sit at zero until the real client is actually in use, which is the honest state to show rather than faking activity.

### 2. LLM at scale

Batched by cluster. Not one call per feature, not one giant prompt.

One call per feature (70 calls on the real catalog) is safest for prompt focus, but that's 70x the latency and cost per evaluation, and most of those prompts would be near-identical shells wrapped around the same page evidence. One mega-prompt is cheap and fast but accuracy drops exactly where it matters most, the model has to hold every feature's judgment call in its head at once, and the catalog "changes over time" per BRIEF §3 so that prompt only keeps growing. It also kills partial caching, any catalog change invalidates the whole thing.

Batching by cluster (roughly a dozen calls on the real catalog, one per `featureClusters` entry) is the middle ground. Each prompt stays focused on a coherent group of related questions, it's an order of magnitude cheaper than per-feature, and a change to one cluster only invalidates that cluster's prompt.

This is a trade I'm making for the production catalog shape (~70 features, ~155 capabilities, 12 clusters), not the toy one. On the toy catalog three clusters means three calls, basically free regardless of strategy, so the toy doesn't actually stress-test this decision.

### 3. Determinism and how you actually test it

Two different things get lumped together under "determinism" and I kept them separate.

Pass-in mode's reproducibility contract (same request, same catalog version, same evaluator version, same answer) is what's actually tested. `tests/test_determinism.py` runs the entire HTTP pipeline three times against a fixed request and checks for byte-identical JSON. Zero network calls, zero variance, because the LLM step runs against `RecordedLLMClient`, a cassette keyed by a hash of the input pages, same idea as VCR-style HTTP cassettes. No temperature setting makes a live model call bit-for-bit reproducible forever, model versions roll, infra changes, nothing genuinely guarantees that. So I didn't try to prove it in CI. What CI does prove is that everything downstream of feature inference, rollup, archetype pick, response shaping, is genuinely pure and stays that way.

Whether the live LLM step itself stays stable over time is a different, statistical question. Temperature 0 and a fixed versioned prompt narrow variance but don't eliminate it. That's not something a unit test can catch. In production I'd run a small golden set of real sites through the live client on a schedule and diff confidence/evidence against a tolerance band (same idea as the fixtures' own `_note` field, illustrative fields get tolerance, not exact match) and alert on drift.

### 4. Confidence

Feature confidence comes straight from the LLM call for that feature, grounded only in the evidence it was shown, the prompt explicitly forbids outside knowledge.

Capability confidence (`requiredCapabilities[].confidence`) is the minimum of the confidences of every present feature that contributed to it. A required capability is only as trustworthy as the weakest piece of evidence behind it. I thought about averaging instead, but averaging lets one low-confidence feature get covered up by a high-confidence co-contributor, which is the wrong direction for a signal meant to warn someone before they build against it.

`overallConfidence` is a criticality-weighted average across all inferred features, must weighted 3x, should 2x, nice 1x. A shaky guess on a "nice" feature shouldn't move the headline number nearly as much as a shaky guess on a "must."

Evidence quality moves all of this indirectly, not through some separate multiplier. A gated site with only marketing-page evidence naturally produces lower per-feature confidence from the LLM, there's just less to go on, and that propagates through the same min/weighted-average logic above. I didn't add a second "evidence quality" score on top of that, it would just be re-deriving the same signal through a harder-to-reason-about path.

### 5. Live investigation strategy

Built this one, not just designed it, see "Bonuses built" further down for the actual implementation and the two bugs it surfaced. Original design thinking:

Start from the domain rather than a fixed URL list. Fetch the homepage first, then use its links plus common conventions (`/pricing`, `/login`, `/signup`, `/docs`) as candidate next hops, ranked by which `sourceType` they most likely represent.

Behave like a researcher, not a scraper. If the homepage links to `/login` and that 401s, don't stop, pivot to public surfaces that usually survive a gate, `/pricing`, docs, the first step of a signup flow (often public even when the app itself isn't).

Bound it hard. `maxPages` and `timeBudgetMs` are first-class stopping conditions checked before every fetch, not just an outer timeout. Respect `robots.txt`. A single evaluation run should look like one polite visitor, not a scraper hammering the site.

On a gated site you can't get into: return 200 with `accessOutcome: "blocked"` or `"partial"`, never treat it as a failure. A login wall is itself weak evidence, it tells you `email-password-login` or `sso-login` is probably present, just not which one, and at lower confidence than seeing the actual form fields would give.

Secrets: if `access.credentials`/`sessionCookies` are supplied, the current crawler does not use them for authentication, they remain request-only data and are never logged, cached, echoed in `trace`, or included in responses. Authentication support is deliberately deferred (see "What I deferred" below). Enforced structurally, `EvaluateAccess` never appears anywhere in `EvaluateResponse`, so there's no code path that could leak it even by accident.

### 6. The contract

`openapi.yaml` is hand-written to match `app/models.py` field for field, not generated off the FastAPI app, so it's reviewable independent of the code. Error taxonomy is small on purpose: `no_evidence` (400, malformed request, neither pass-in content nor live crawl requested), `unknown_catalog_version` (400, a pinned version that doesn't exist), `catalog_unavailable` (502, catalog endpoint unreachable with no cached copy to fall back on). A gated or inaccessible site is deliberately not in that list, that's a 200 with a low-confidence, well-labelled result, per BRIEF §4. `trace` is optional, null when there's nothing to report, populated with real diagnostics when live crawling actually ran, see below.

## Bonuses built

All three bonus items from BRIEF §6 are implemented and tested, not just designed on paper.

**Catalog-API resilience** (`app/catalog.py`). A transient failure, connection error, timeout, 5xx, gets retried with exponential backoff (`CATALOG_RETRY_ATTEMPTS` / `CATALOG_RETRY_BASE_DELAY_SECONDS`, both configurable via env) before it's treated as a real failure. A confirmed 404 is never retried, that's a real answer ("this version doesn't exist"), not a glitch, retrying it would just waste time. Once retries are exhausted, "latest" falls back to the last known good cached version if there is one. A pinned version with nothing cached has nowhere to fall back to, so that surfaces honestly as a 502. `tests/test_catalog_resilience.py` uses a fake transport scripted to fail N times before succeeding, or fail permanently, so this is actually exercised, not just asserted.

**Concurrency limits, structured logs, and token/cost accounting** (`app/llm.py`, `app/metrics.py`). Feature-cluster LLM calls don't depend on each other, so they now run concurrently via `asyncio.gather`, bounded by a semaphore (`LLM_MAX_CONCURRENT_CLUSTERS`, default 4) instead of running fully sequential or fully unbounded. Unbounded concurrency on the real ~12-cluster catalog would fire a burst of simultaneous requests at the LLM provider on every single evaluation, which is asking for rate limit trouble. Every cluster call now emits a structured JSON log line, start, validation failure, retry outcome, fallback, completion with latency, so a real log aggregator could reconstruct what happened without a custom parser. Token usage comes straight off the API response (`resp.usage.input_tokens` / `output_tokens`) and gets recorded on every call regardless of whether that call's response actually validates, because the tokens were spent either way. `GET /internal/guardrail-metrics` now reports totals plus an estimated cost. Cost estimation is opt-in and zero by default (`LLM_INPUT_COST_PER_MILLION_TOKENS` / `LLM_OUTPUT_COST_PER_MILLION_TOKENS`), I didn't want to ship a hard-coded per-token price that would just go stale. Concurrency and token recording both have dedicated tests against a fake transport, including one that proves calls actually overlap when the limit allows it, not just that they're capped when it doesn't.

**A thin live-mode crawl** (`app/live_crawler.py`). Given just a domain, it fetches the homepage, checks `robots.txt` (a confirmed disallow is never fetched, a missing or unreadable `robots.txt` fails open instead of blocking the whole crawl, more on why that had to be explicit below), pulls out the links, ranks them by how well they match known surface types (pricing, login, signup, docs, the same taxonomy `SourceType` already uses), and fetches the top few. It stops on whichever budget runs out first, `maxPages` or `timeBudgetMs`, checked before every single fetch rather than as an outer timeout wrapper. A gated homepage or a single gated follow-link gets reported honestly through `accessOutcome`, never treated as a crash, same principle as pass-in mode's gated-site handling. And it doesn't reimplement feature inference, crawled pages become ordinary `Page` objects and flow straight into the same pass-in evidence pipeline in `main.py`. One code path for "does this evidence show feature X," not two to keep in sync.

This one actually caught two real bugs while I was testing it, which is worth being upfront about rather than glossing over.

First, Python's own `RobotFileParser.can_fetch()` defaults to disallow when it's never successfully parsed any rules, not "fail open" like I'd assumed going in. That meant a site with no `robots.txt` at all was getting treated as fully blocked, which is backwards. I caught it with a unit test that expected a crawl to succeed against a site with no `robots.txt`, and it didn't.

Second, running the crawler against an actual site (pypi.org, not one of my fixtures) turned up a duplicate-fetch bug, a homepage that links back to itself, like a logo pointing at `/`, was getting treated as a separate candidate link and refetched. None of my hand-written fixture pages happened to self-link, so no test caught it until I ran the thing against something real. Which is kind of the whole point, fixtures only catch what you thought to write into them.

Both are fixed now and have regression tests in `tests/test_live_crawler.py`. I re-ran the crawler against pypi.org afterward to confirm the fix actually holds, not just that the new test passes.

## What I deferred, and why

A second "why isn't this feature present" evidence line for the not-present case. Right now a not-present feature gets a generic "no supporting evidence" string from the recorded client. The real Anthropic client's prompt already asks for this, I just didn't spend time polishing the not-present phrasing since it's illustrative and graded with tolerance anyway.

Guardrail metrics are in-process only. `GET /internal/guardrail-metrics` reads an in-memory counter, no auth, no persistence, no multi-instance aggregation. Fine for proving the validation boundary and cost tracking actually do something during a take-home, a real deployment needs this pushed to a proper metrics backend and that endpoint either removed or properly scoped.

Live crawl is deliberately thin. No JS rendering (a client-rendered SPA's homepage might show almost no text), no auth attempt even when credentials are supplied, no sitemap parsing, no pagination-following. That's the scope line the brief actually asked for, "a couple of obvious surfaces." Fuller version below.

## What I'd do with two more weeks

1. Golden-set regression harness for the live LLM client, a few dozen real hand-labelled sites, run nightly, diffing confidence/evidence against tolerance bands, alerting on drift. This is the thing that actually protects the fuzzy half of the service once it's not just two hand-built fixtures.
2. Widen the live crawler: JS rendering for client-side-heavy sites (would need a real browser, Playwright probably, not just httpx and stdlib HTML parsing), sitemap.xml as an extra candidate source alongside homepage links, pagination-following for docs/help centers where the useful evidence often isn't on page one.
3. Push metrics to a real backend, Prometheus or StatsD or similar, instead of the in-process counters `/internal/guardrail-metrics` reads today, and load-test the cluster-batched, concurrency-bounded LLM strategy against something closer to the real 70/155/12 catalog shape to check my latency and cost assumptions actually hold up.
4. Circuit-breaking on the catalog fetch, on top of the retry/backoff that's already there. Retrying an endpoint that's been down for an extended stretch just wastes time that would be better spent falling back immediately.
5. Tighten the "extra present feature" archetype penalty in `app/rollup.py`. Right now it's a flat weight. On the real catalog, with a lot more archetypes, I'd want that tuned against a labelled set rather than the two fixtures I had to reverse-engineer it against.

Where I'd have spent an 8th hour: sharpening the not-present evidence strings from the real LLM client, and a couple more unit tests around archetype selection edge cases, a site with no clearly-dominant archetype, roughly equal weak signal everywhere.

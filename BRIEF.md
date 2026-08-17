# Take-home: Site Capability Evaluator

**Time budget:** 5–7 hours, including time with a coding agent.
**What you ship:** a small Git repo + a short Loom (≤ 10 min) walking through what you built and how you'd take it to production.

> This is a real problem we work on, deliberately shrunk to fit a take-home. We are **not** looking for a finished product. We are looking for the **technical judgment** you apply along the way: where you spend your hours, what you decide to build vs. defer, and how you reason about the parts we left open. A working app that ignores the design questions in [§5](#5-the-decisions-we-want-you-to-make) will score *lower* than a half-finished one that engages with them well.
>
> **Using a coding agent (Claude Code, Cursor, etc.) is expected and encouraged.** The catch: this brief is written so that pasting it wholesale into an agent and shipping the output will *not* produce a good submission. The decisions that matter are left to you on purpose, and the toy data hides the constraints that bite at real scale. Your README and Loom must show *your* thinking, not the agent's defaults.

---

## 1. Context — the problem this service exists to solve

We run an autonomous browser agent that explores and tests web applications. Whether our agent can usefully test a given customer's product depends on **what that product requires** — a static marketing site needs very different abilities than a multi-step signup flow behind corporate SSO.

We model this with three object types:

- **Capability** — an ability *our agent* has, e.g. "fill multi-step forms", "log in via SSO", "traverse iframes". Each capability has a maturity **level 0–3**.
- **Feature** — a characteristic *a customer's product* might have, e.g. "has a multi-step signup", "is behind a login wall". Each feature **requires** certain capabilities at a minimum level.
- **Archetype** — a common bundle of features, e.g. "SaaS application", "E-commerce".

When a prospect comes in, we want to answer one question automatically:

> **Given this company's website, what would our agent need in order to test it — which capabilities, at what minimum level?**

That is the *only* question this service answers. It does **not** decide whether our agent is good enough yet, or whether we can serve the customer — that scoring happens elsewhere, downstream of your output. You answer exactly: ***"What does this site need?"***

You consume one API we provide (the **Catalog**, [§3](#3-the-catalog-api-provided)) and you expose one API (**Evaluate**, [§4](#4-the-api-you-build)).

---

## 2. What you're building

A **stateless HTTP service** — the **Evaluator** — that:

1. Receives an evaluation request for a site ([§4](#4-the-api-you-build)).
2. Gets **evidence** about the site. Two modes:
   - **Pass-in mode** — the caller hands you captured pages; you do no fetching. This is the **reproducible** path used for tests and re-scoring. **Build this well; it is the core deliverable.**
   - **Live mode** — given just a domain, you investigate it yourself. This is the harder, real-world path. We expect you to *design* it and discuss it thoroughly; a basic implementation is a bonus, not a requirement (see [§6](#6-scope-what-to-build-vs-defer)).
3. Fetches the current **Catalog** ([§3](#3-the-catalog-api-provided)) and caches it.
4. Infers which catalog **features** the site has.
5. Derives the **required capabilities** from the catalog's feature→capability map.
6. Returns a structured result with confidence, evidence, and reasoning.

**Hard boundary — do not cross it.** The Evaluator never reads, stores, or reasons about our agent's *current* capability levels, and never computes a "readiness" or "fit" score. It is a pure function of *(site evidence, catalog version, evaluator version)*. If you ever feel you need our current levels, you've crossed the line. This boundary is what keeps the service independently testable — and we check for it.

---

## 3. The Catalog API (provided)

The catalog is **versioned and immutable**: a given version always returns identical content, and it can grow or change between versions. **Do not hard-code catalog contents** — fetch them at runtime and cache by version.

For this exercise we give you a **small, fictional sample catalog**: [`fixtures/catalog.json`](fixtures/catalog.json) — **10 features, 13 capabilities, 3 archetypes**. Stand it up however you like (serve the file, or read it directly — your choice; document it).

> ⚠️ **Design for the real thing, not the toy.** The production catalog is roughly **70 features, ~155 capabilities, 12 archetypes**, and it changes over time. The sample is small enough that almost any approach "works" on it — but some approaches fall over at real scale (latency, cost, prompt size, accuracy). Assume the real catalog when you make design decisions, and call out in your README where the toy let you cut a corner you wouldn't cut in production.

### Shapes

```ts
interface Feature {
  id: string;
  name: string;
  question: string;           // natural-language test for "does the product have this?"
  clusterId: string;
  requires: {                 // which capabilities this feature demands, and how good they must be
    capabilityId: string;
    minLevel: 0 | 1 | 2 | 3;
    criticality: "must" | "should" | "nice";
  }[];
}

interface Capability {
  id: string;
  name: string;
  description: string;
  areaId: string;
  acceptanceCriteria: { level: 0 | 1 | 2 | 3; text: string }[];   // what each level means
}

interface Archetype {
  id: string;
  name: string;
  description: string;
  features: { featureId: string; criticality: "must" | "should" | "nice" }[];
}
```

The catalog endpoint you should model (you may mock it):

```
GET {CATALOG_BASE}/v1/catalog            # latest
GET {CATALOG_BASE}/v1/catalog?version=<v>
→ 200 { version, featureClusters[], features[], capabilities[], archetypes[], investmentAreas[] }
```

The catalog contains **definitions only** — there are deliberately **no** "current level" fields (see the hard boundary in §2).

---

## 4. The API you build

```
POST /v1/evaluate
Content-Type: application/json
```

No auth in v1 — the service runs inside our internal network; we add network/auth controls around it later.

We give you the **shape that matters** below. The exact edges — error taxonomy, how much you expose in a debug `trace`, optional fields — are **yours to design and document** (your OpenAPI doc is the source of truth, not this prose).

### Request (essentials)

```ts
interface EvaluateRequest {
  site: { domain: string; name?: string; archetypeHint?: string };

  // PASS-IN MODE: if present, work ONLY from these pages — no fetching. Reproducible path.
  content?: {
    pages: {
      url: string;
      sourceType: "app" | "marketing" | "pricing" | "docs" | "login" | "signup" | "external" | "other";
      text?: string;          // extracted readable text — the preferred input
      html?: string;
      title?: string;
      httpStatus?: number;
      notes?: string;
    }[];
    extraSignals?: Record<string, unknown>;
  };

  // LIVE MODE access (optional): everything here is SECRET — see §7.
  access?: {
    loginUrl?: string;
    credentials?: { username?: string; password?: string; secrets?: Record<string, string> };
    sessionCookies?: { name: string; value: string; domain: string }[];
    notes?: string;
  };

  options?: {
    liveCrawl?: { enabled?: boolean; maxPages?: number; timeBudgetMs?: number };
    model?: string;
  };

  catalogVersion?: string;   // pin a version; omit → latest (and echo what you used)
}
```

### Response (essentials)

```ts
interface EvaluateResponse {
  catalogVersion: string;       // the version actually used
  evaluatorVersion: string;     // your build version — part of the determinism key
  archetype: { id: string; confidence: number };
  inferredFeatures: {
    featureId: string;
    present: boolean;
    criticality: "must" | "should" | "nice";   // carried through from the catalog feature
    confidence: number;                          // 0..1
    evidence: string;                            // one line, citing the source it came from
  }[];
  requiredCapabilities: {
    capabilityId: string;
    minLevel: 0 | 1 | 2 | 3;
    criticality: "must" | "should" | "nice";
    confidence: number;                          // 0..1
    sourceFeatureIds: string[];                  // which present features drove this
    reasoning: string;
  }[];
  overallConfidence: number;
  investigation: {
    mode: "live" | "passed-in" | "mixed";
    accessOutcome: "authenticated" | "public-only" | "partial" | "blocked";
    evidenceSources: { type: string; url?: string; used: boolean; note?: string }[];
    pagesUsed: number;
  };
  trace?: Record<string, unknown>;   // optional debug — MUST NOT contain credentials/cookies
}
```

### How `requiredCapabilities` relates to `inferredFeatures`

The required-capability set is **derived from the present features** via the catalog's `requires` map — and it must be **exact and reproducible**, not a judgment call. The two worked fixtures in `fixtures/sites/` pin down the intended semantics (including a deliberately subtle case). **Read them, infer the rule, and make your implementation reproduce them.** Being able to state the rule you inferred — and why level and criticality behave the way they do when several features demand the same capability — is part of what we're assessing.

### A gated site is **not** an error

If the product is behind a login you can't pass, still return **200** with `accessOutcome: "public-only"` (or `"blocked"`) and confidence that reflects the weaker evidence. Reserve non-200s for genuinely malformed requests, an unreachable catalog, or a pinned catalog version that doesn't exist. The exact error taxonomy is yours to design.

---

## 5. The decisions we want you to make

This is the part we care about most. The brief deliberately does **not** answer these for you. Pick a position on each, implement accordingly, and **defend it in your README and Loom**. There are no single right answers — we're assessing the quality of the reasoning and whether the choice fits the constraints.

1. **The fuzzy / deterministic boundary.** Part of this problem is genuine judgment ("does this site have multi-step forms?"); part of it must be exact and reproducible. **Where do you draw the line, and what runs on which side?** What does the LLM decide, and what does plain code decide — and why?

2. **How you use the LLM at scale.** The toy catalog has 10 features; the real one has ~70 and ~155 capabilities. One LLM call per feature? One mega-prompt? Batched by cluster? Something else? Justify the **accuracy / latency / cost** trade-off *at production scale*, not on the toy.

3. **Determinism & testability of an LLM-backed service.** Pass-in mode must be **reproducible**: the same request + catalog version + evaluator version should yield the same answer (within stated tolerances). How do you achieve that, and how do you *test* it in CI without flaking? Ship at least one test that demonstrates it.

4. **Confidence.** Where do the numbers come from, and what makes them mean something? How does evidence quality (authenticated product vs. a guess from the domain) move them? How do per-feature confidences roll up into `requiredCapabilities` and `overallConfidence`?

5. **Live investigation strategy (mostly design).** A good evaluator behaves like a *researcher*, not a fixed-URL scraper: it decides what's worth fetching for *this* site, and when a product is gated it pivots to public surfaces (pricing, login/signup structure, docs, third-party listings) rather than giving up. How would you build that? How do you bound it (time, pages, politeness/robots) and keep it safe? You may implement a thin version; mostly we want the design.

6. **The contract.** You own the HTTP surface. Ship an **OpenAPI 3.1** document that is the source of truth and stays in sync with the implementation. Decide your error taxonomy, what (if anything) goes in `trace`, and which fields are required vs optional.

You won't perfectly resolve all six in 5–7 hours. **Resolve some in code, the rest in words** — explicitly saying "here's what I'd do and why I didn't get to it" is a strong signal, not a weak one.

---

## 6. Scope — what to build vs. defer

Spend your hours where they show judgment, not plumbing. Suggested split:

**Must (the core):**
- `POST /v1/evaluate` working end-to-end in **pass-in mode**: feature inference → deterministic capability rollup → structured response, against the sample catalog.
- Catalog fetched at runtime and **cached by version**.
- The deterministic rollup **reproduces both worked fixtures** exactly.
- A **determinism test** for the pass-in path, and a **unit test** for the rollup logic.
- Structured errors; `GET /health` and `GET /version`.
- **OpenAPI 3.1** doc for your surface.
- A **Dockerfile** that starts clean from env config alone.
- README covering your decisions ([§5](#5-the-decisions-we-want-you-to-make)), the LLM prompt(s) you used, and how to run it.

**Bonus (only if the core is solid):**
- A thin **live-mode** crawl for a public site (homepage + a couple of obvious surfaces).
- Catalog-API resilience (serve from cache when it blips).
- Concurrency limits, structured logs, token/cost accounting.

**Explicitly out of scope — do not build:**
- Any "readiness"/"fit" scoring, or anything that reads our agent's current levels (the §2 boundary).
- Real login/MFA automation against real third-party sites. (Design it in words; don't burn hours fighting captchas.)
- A UI, a database, persistence of any kind, CRM/account integrations.
- Authoring or editing the catalog — you only read it.

If you find yourself gold-plating one of the bonuses while the core or the §5 write-up is thin, stop and rebalance.

---

## 7. Constraints that actually matter

- **Stateless.** No database. The only state allowed is an in-memory catalog cache keyed by version.
- **Secrets in memory only.** `access.credentials` / `sessionCookies` (and anything fetched with them) are secrets: **never** log, persist, cache, echo in `trace`/responses, or leak through error paths; drop them at end of request. We review for this.
- **Provider-agnostic-ish LLM.** Configure provider/model/key via env — nothing hard-coded. A current Claude model is a fine default. (If you build with Claude, see the project's `claude-api` reference for current model IDs and tool-use patterns.)
- **Config via env**, no host assumptions: e.g. `CATALOG_BASE`, `LLM_API_KEY` (+ provider/model). Container starts clean from env alone.

---

## 8. Deliverables

1. **A Git repo** — source, tests, `Dockerfile`, `openapi.yaml`.
2. **README** — how to build/run/configure; the LLM prompt(s) you used; and a clear write-up of your [§5](#5-the-decisions-we-want-you-to-make) decisions, including what you'd do differently at production scale and what you deferred.
3. **A Loom (≤ 10 min)** — see below.

### What to cover in the Loom

Walk us through it as if onboarding a teammate who'll take it to production. Hit:
- A quick demo: a fixture in, the structured result out.
- The **fuzzy/deterministic boundary** you chose and why.
- Your **LLM-at-scale** strategy and the trade-off you're making.
- How you made the pass-in path **reproducible and tested**.
- Your **live-investigation** design — including what happens on a gated site you can't log into.
- What you'd do with **two more weeks** to productionize this: what's missing, what worries you, what you'd harden first.

---

## 9. How we evaluate

We weight **judgment and reasoning** above feature count. Roughly:

| Area | What we look for |
|---|---|
| **Design judgment (§5)** | Clear, defensible positions on the open decisions; choices that fit production scale, not just the toy. |
| **Correctness** | The deterministic rollup is exact and reproduces the fixtures, incl. the subtle case. Determinism on pass-in holds. |
| **The boundary & secrets** | No readiness scoring / current-level leakage; credentials never persisted or echoed. |
| **Code quality** | Readable, appropriately tested, the deterministic core is cleanly separated from the LLM/fuzzy part. |
| **Contract** | OpenAPI matches the implementation; sensible errors; gated-site case handled as 200. |
| **Communication** | README + Loom make your decisions and trade-offs legible. |

What will *not* impress us: a large surface area of half-working features, an LLM doing arithmetic that should be deterministic, confidence numbers with no rationale, or a submission that reads like an unedited agent transcript.

Good luck — and tell us where you'd have spent the 8th hour.

---

## Appendix — worked example (pass-in)

`fixtures/sites/acme-hr.input.json` → `…/acme-hr.expected.json` shows a SaaS signup/login site.
`fixtures/sites/shopwave.input.json` → `…/shopwave.expected.json` shows an e-commerce checkout — and contains the **subtle rollup case** (a capability whose required *level* and required *criticality* come from different features). Make sure your implementation reproduces both. The `_note` and `_walkthrough` keys in the expected files explain exactly what is graded as a hard contract vs. what is illustrative.

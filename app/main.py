from __future__ import annotations
import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from .config import settings
from .catalog import catalog_client, CatalogUnavailableError, CatalogVersionNotFoundError
from .live_crawler import crawl_site
from .llm import get_llm_client
from .rollup import compute_required_capabilities, pick_archetype, overall_confidence
from .models import (
    EvaluateRequest, EvaluateResponse, InferredFeature, Investigation, EvidenceSource,
    ErrorResponse,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")  # app.llm emits pre-formatted JSON lines

app = FastAPI(title="Site Capability Evaluator", version=settings.evaluator_version)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/version")
async def version():
    return {"evaluatorVersion": settings.evaluator_version}


@app.get("/internal/guardrail-metrics")
async def guardrail_metrics_endpoint():
    """Read-only visibility into the LLM boundary validation layer (app/llm.py,
    app/metrics.py) - how often the model's response failed structural
    validation, how often a retry recovered it, how often we fell back to a
    conservative default, and the confidence distribution actually returned.
    Not authenticated/scoped for production (see README) - this is the seam
    where a real metrics backend would plug in, exposed here so it's inspectable
    during the take-home rather than invisible."""
    from .metrics import guardrail_metrics
    return guardrail_metrics.snapshot(
        input_cost_per_million=settings.llm_input_cost_per_million_tokens,
        output_cost_per_million=settings.llm_output_cost_per_million_tokens,
    )


def _assign_criticality(inferred: list[InferredFeature], archetype_id: str, catalog) -> None:
    """Criticality for an inferred feature is carried through from how the SITE'S
    matched archetype declares that feature - not a property of the feature itself
    (the catalog only attaches criticality to a *requires* edge or an *archetype*
    membership, never to the feature in isolation). A present feature the matched
    archetype doesn't mention gets "nice" by default: the archetype told us nothing
    about how much it matters, so we don't let it drive a "must" anywhere downstream.
    This rule was inferred by reproducing both worked fixtures exactly (see README §5)."""
    archetype = next((a for a in catalog.archetypes if a.id == archetype_id), None)
    declared = {ref.featureId: ref.criticality for ref in archetype.features} if archetype else {}
    for f in inferred:
        f.criticality = declared.get(f.featureId, "nice")


@app.post("/v1/evaluate", response_model=EvaluateResponse, responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}})
async def evaluate(req: EvaluateRequest):
    # --- fetch + cache catalog ---
    try:
        catalog = await catalog_client.get(req.catalogVersion)
    except CatalogVersionNotFoundError:
        raise HTTPException(status_code=400, detail={"error": "unknown_catalog_version", "message": f"catalogVersion {req.catalogVersion!r} was not found"})
    except CatalogUnavailableError:
        raise HTTPException(status_code=502, detail={"error": "catalog_unavailable", "message": "could not reach the catalog service"})

    # --- determine mode / gather evidence ---
    has_content = bool(req.content and req.content.pages)
    live_requested = bool(req.options and req.options.liveCrawl and req.options.liveCrawl.enabled)

    if has_content and not live_requested:
        mode = "passed-in"
    elif has_content and live_requested:
        mode = "mixed"
    elif live_requested:
        mode = "live"
    else:
        # Nothing to work with. This is a malformed request, not a gated site.
        raise HTTPException(status_code=400, detail={"error": "no_evidence", "message": "provide content.pages (pass-in mode) or enable options.liveCrawl (live mode)"})

    pages = list(req.content.pages) if req.content else []

    trace: dict | None = None

    if live_requested:
        live_opts = req.options.liveCrawl
        max_pages = live_opts.maxPages or settings.default_live_max_pages
        time_budget_ms = live_opts.timeBudgetMs or settings.default_live_time_budget_ms
        crawl_result = await crawl_site(
            req.site.domain, max_pages=max_pages, time_budget_ms=time_budget_ms
        )
        pages.extend(crawl_result.pages)
        trace = {
            "liveCrawl": {
                "pagesFetched": len(crawl_result.pages),
                "robotsBlockedCount": crawl_result.robots_blocked_count,
                "maxPages": max_pages,
                "timeBudgetMs": time_budget_ms,
            }
        }
        if mode == "live":
            access_outcome = crawl_result.access_outcome
        else:  # mixed: passed-in content is guaranteed present (mode wouldn't be
               # "mixed" otherwise), so even a fully-blocked live crawl still
               # leaves us with public-only evidence from the passed-in pages
            access_outcome = crawl_result.access_outcome if crawl_result.pages else "public-only"
    else:
        access_outcome = "public-only" if pages else "blocked"

    evidence_sources = [
        EvidenceSource(type=p.sourceType, url=p.url, used=True) for p in pages
    ]

    # --- fuzzy step: LLM infers feature presence from evidence only ---
    llm = get_llm_client(settings)
    inferred = await llm.infer_features(catalog.features, pages)

    # --- deterministic step: archetype, then criticality carry-through, then rollup ---
    archetype_result = pick_archetype(catalog, inferred)
    _assign_criticality(inferred, archetype_result.id, catalog)

    present_ids = {f.featureId for f in inferred if f.present}
    confidence_by_feature = {f.featureId: f.confidence for f in inferred}
    required_caps = compute_required_capabilities(catalog, present_ids, confidence_by_feature)

    response = EvaluateResponse(
        catalogVersion=catalog.version,
        evaluatorVersion=settings.evaluator_version,
        archetype=archetype_result,
        inferredFeatures=inferred,
        requiredCapabilities=required_caps,
        overallConfidence=overall_confidence(inferred),
        investigation=Investigation(
            mode=mode,
            accessOutcome=access_outcome,
            evidenceSources=evidence_sources,
            pagesUsed=len(pages),
        ),
        trace=trace,
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=detail)

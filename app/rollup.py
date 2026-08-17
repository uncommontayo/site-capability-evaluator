"""Deterministic, pure functions only. No LLM calls, no network, no I/O.

This module is the "exact and reproducible" half of the service (see README §5.1).
Given the catalog and the set of *present* features, it must derive the same
requiredCapabilities every time - that determinism is graded directly against
fixtures/sites/*.expected.json.

Rule inferred from the two worked fixtures (see BRIEF.md appendix):
  For a capability required by more than one present feature:
    - minLevel     = MAX of the minLevel each contributing feature demands
    - criticality  = STRICTEST of the criticality each contributing feature demands
                      (must > should > nice)
  These two roll-ups are independent of each other - a capability's level and its
  criticality can trace back to *different* source features (the ShopWave
  text-input case: has-payment-forms sets the level to 3 but only as "should";
  has-search/has-simple-forms set it to "must" but only at level 2. Result:
  level 3, must).
"""
from __future__ import annotations
from .models import Catalog, ArchetypeFeatureRef, ArchetypeResult, InferredFeature, RequiredCapability

_CRITICALITY_RANK = {"nice": 0, "should": 1, "must": 2}


def _strictest(criticalities: list[str]) -> str:
    return max(criticalities, key=lambda c: _CRITICALITY_RANK[c])


def compute_required_capabilities(
    catalog: Catalog, present_feature_ids: set[str], confidence_by_feature: dict[str, float]
) -> list[RequiredCapability]:
    features_by_id = {f.id: f for f in catalog.features}
    contributions: dict[str, list[tuple[str, int, str]]] = {}

    # Iterate in sorted order, not set order: set iteration depends on string hashing,
    # which Python randomizes per process (PYTHONHASHSEED). minLevel/criticality/
    # sourceFeatureIds are all order-independent (max/strictest/sorted), but the
    # `reasoning` string renders `contribs` in list order - so unsorted iteration made
    # that field differ between two processes given identical input, breaking the
    # cross-restart determinism guarantee in README §5.3.
    for fid in sorted(present_feature_ids):
        feature = features_by_id.get(fid)
        if feature is None:
            continue  # unknown feature id (e.g. stale catalogVersion mismatch); ignore rather than crash
        for req in feature.requires:
            contributions.setdefault(req.capabilityId, []).append((fid, req.minLevel, req.criticality))

    results: list[RequiredCapability] = []
    for cap_id, contribs in contributions.items():
        min_level = max(c[1] for c in contribs)
        criticality = _strictest([c[2] for c in contribs])
        source_feature_ids = sorted({c[0] for c in contribs})

        # Capability confidence: the MIN of the confidences of the features that
        # contributed to it. A required capability is only as trustworthy as the
        # weakest piece of evidence that put it there (see README §5.4).
        confidences = [confidence_by_feature.get(fid, 0.0) for fid in source_feature_ids]
        cap_confidence = round(min(confidences), 3) if confidences else 0.0

        level_parts = ", ".join(f"{fid}@{lvl}({crit})" for fid, lvl, crit in contribs)
        reasoning = (
            f"minLevel=max({level_parts}); criticality=strictest of the same set."
        )

        results.append(
            RequiredCapability(
                capabilityId=cap_id,
                minLevel=min_level,  # type: ignore[arg-type]
                criticality=criticality,  # type: ignore[arg-type]
                confidence=cap_confidence,
                sourceFeatureIds=source_feature_ids,
                reasoning=reasoning,
            )
        )

    return sorted(results, key=lambda r: r.capabilityId)


_ARCHETYPE_WEIGHT = {"must": 3, "should": 2, "nice": 1}


_EXTRA_FEATURE_PENALTY_WEIGHT = 2  # flat "should"-equivalent penalty per unexplained present feature


def pick_archetype(catalog: Catalog, inferred_features: list[InferredFeature]) -> ArchetypeResult:
    """Deterministic given the (fuzzy) per-feature confidences already produced by the
    LLM step. We deliberately do NOT make a second LLM call to pick the archetype -
    it is fully derivable from inferredFeatures, so a second call would just add
    latency/cost for no accuracy gain (see README §5.2).

    A pure "how many of the archetype's declared features are present" score is not
    enough: a small archetype like marketing-site is trivially satisfied by any site
    that happens to have a couple of simple forms, even an e-commerce checkout. So the
    score also penalises (a) declared *must* features that are missing, and (b) present
    features the archetype does not mention at all (a strong sign it's the wrong
    archetype, not just an incomplete one)."""
    present_conf = {f.featureId: f.confidence for f in inferred_features if f.present}

    best: ArchetypeResult | None = None
    best_score = -1.0
    for archetype in catalog.archetypes:
        declared_ids = {ref.featureId for ref in archetype.features}
        matched_weight = 0.0
        missing_must_weight = 0.0
        total_weight = 0.0

        for ref in archetype.features:
            weight = _ARCHETYPE_WEIGHT[ref.criticality]
            total_weight += weight
            if ref.featureId in present_conf:
                matched_weight += weight * present_conf[ref.featureId]
            elif ref.criticality == "must":
                missing_must_weight += weight

        extra_penalty = sum(
            _EXTRA_FEATURE_PENALTY_WEIGHT * conf
            for fid, conf in present_conf.items()
            if fid not in declared_ids
        )

        raw_score = matched_weight - missing_must_weight - extra_penalty
        normalized = max(0.0, raw_score) / total_weight if total_weight else 0.0
        normalized = min(1.0, normalized)

        if normalized > best_score:
            best_score = normalized
            best = ArchetypeResult(id=archetype.id, confidence=round(normalized, 2))

    assert best is not None, "catalog has no archetypes"
    return best


def overall_confidence(inferred_features: list[InferredFeature]) -> float:
    """Confidence-weighted by criticality: a low-confidence 'must' should pull the
    overall number down harder than a low-confidence 'nice'."""
    weight_map = {"must": 3, "should": 2, "nice": 1}
    total_weight = sum(weight_map[f.criticality] for f in inferred_features) or 1
    weighted = sum(weight_map[f.criticality] * f.confidence for f in inferred_features)
    return round(weighted / total_weight, 3)

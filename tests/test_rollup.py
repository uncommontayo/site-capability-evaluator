import json
import os
import pytest
from app.models import Catalog, InferredFeature
from app.rollup import compute_required_capabilities, pick_archetype

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


@pytest.fixture
def catalog() -> Catalog:
    with open(os.path.join(FIXTURES_DIR, "catalog.json"), encoding="utf-8") as fh:
        return Catalog.model_validate(json.load(fh))


def _load_expected(name: str) -> dict:
    with open(os.path.join(FIXTURES_DIR, "sites", f"{name}.expected.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("name", ["acme-hr", "shopwave"])
def test_required_capabilities_matches_fixture_exactly(catalog, name):
    """This is the graded contract: capabilityId / minLevel / criticality /
    sourceFeatureIds must match exactly (order-independent). confidence/reasoning
    are ours to design and are not checked here."""
    expected = _load_expected(name)
    present_ids = {f["featureId"] for f in expected["inferredFeatures"] if f["present"]}
    confidences = {f["featureId"]: f["confidence"] for f in expected["inferredFeatures"]}

    got = compute_required_capabilities(catalog, present_ids, confidences)

    got_set = {(r.capabilityId, r.minLevel, r.criticality, tuple(sorted(r.sourceFeatureIds))) for r in got}
    expected_set = {
        (r["capabilityId"], r["minLevel"], r["criticality"], tuple(sorted(r["sourceFeatureIds"])))
        for r in expected["requiredCapabilities"]
    }
    assert got_set == expected_set


def test_shopwave_text_input_subtle_case(catalog):
    """The rollup must roll up minLevel and criticality INDEPENDENTLY. has-payment-forms
    demands text-input @3 but only as 'should'; has-search / has-simple-forms demand it
    @2 but as 'must'. The correct answer takes the max level (3) AND the strictest
    criticality (must) - a naive "take everything from whichever feature is strictest"
    shortcut would wrongly cap the level at 2."""
    expected = _load_expected("shopwave")
    present_ids = {f["featureId"] for f in expected["inferredFeatures"] if f["present"]}
    confidences = {f["featureId"]: f["confidence"] for f in expected["inferredFeatures"]}

    got = compute_required_capabilities(catalog, present_ids, confidences)
    text_input = next(r for r in got if r.capabilityId == "text-input")

    assert text_input.minLevel == 3
    assert text_input.criticality == "must"
    assert set(text_input.sourceFeatureIds) == {"has-search", "has-payment-forms", "has-simple-forms"}


@pytest.mark.parametrize("name,expected_archetype", [("acme-hr", "saas-app"), ("shopwave", "ecommerce")])
def test_archetype_selection_picks_the_right_bucket(catalog, name, expected_archetype):
    """Archetype confidence is explicitly illustrative/LLM-tolerant per the fixtures'
    own _note; the archetype ID it lands on is not, so that's what we assert on."""
    expected = _load_expected(name)
    inferred = [
        InferredFeature(featureId=f["featureId"], present=f["present"], criticality=f["criticality"],
                         confidence=f["confidence"], evidence=f.get("evidence", ""))
        for f in expected["inferredFeatures"]
    ]
    result = pick_archetype(catalog, inferred)
    assert result.id == expected_archetype


def test_unknown_present_feature_id_is_ignored_not_fatal(catalog):
    """A stale/unknown feature id (e.g. a mismatched catalogVersion) should not crash
    the rollup - it should be skipped."""
    got = compute_required_capabilities(catalog, {"totally-made-up-feature"}, {})
    assert got == []

"""Cross-process determinism check for the rollup logic.

tests/test_determinism.py proves pass-in mode is reproducible across repeated
calls WITHIN one process. That's necessary but not sufficient: Python
randomizes string-hash seeding per process by default (PYTHONHASHSEED), so a
bug in set-iteration order can pass every in-process test and still produce
different bytes after a real restart. That's exactly what happened here - see
app/rollup.py's compute_required_capabilities, which now iterates
sorted(present_feature_ids) instead of the raw set for this reason.

This test subprocesses the rollup computation under several different hash
seeds and confirms the output is byte-identical across all of them - the
thing that would actually have caught the bug before it shipped.
"""
import hashlib
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

_SCRIPT = """
import json, sys, hashlib
sys.path.insert(0, {repo_root!r})
from app.models import Catalog
from app.rollup import compute_required_capabilities, pick_archetype, overall_confidence

catalog = Catalog.model_validate(json.load(open({catalog_path!r})))
expected = json.load(open({expected_path!r}))
present_ids = {{f["featureId"] for f in expected["inferredFeatures"] if f["present"]}}
conf = {{f["featureId"]: f["confidence"] for f in expected["inferredFeatures"]}}

caps = compute_required_capabilities(catalog, present_ids, conf)
serialized = json.dumps([c.model_dump() for c in caps], sort_keys=True)
print(hashlib.sha256(serialized.encode()).hexdigest())
"""


def _rollup_hash_under_seed(fixture_name: str, seed: str) -> str:
    catalog_path = os.path.join(REPO_ROOT, "fixtures", "catalog.json")
    expected_path = os.path.join(REPO_ROOT, "fixtures", "sites", f"{fixture_name}.expected.json")
    script = _SCRIPT.format(repo_root=REPO_ROOT, catalog_path=catalog_path, expected_path=expected_path)
    env = {**os.environ, "PYTHONHASHSEED": seed}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"subprocess failed (seed={seed}): {result.stderr}"
    return result.stdout.strip()


def test_rollup_output_is_identical_across_process_hash_seeds():
    """The regression test for the bug this file's docstring describes: same
    fixture, several distinct PYTHONHASHSEED values, each in its own real
    subprocess - the output hash must be identical across all of them."""
    seeds = ["1", "2", "3", "42", "1337"]
    for fixture_name in ["acme-hr", "shopwave"]:
        hashes = {seed: _rollup_hash_under_seed(fixture_name, seed) for seed in seeds}
        distinct_hashes = set(hashes.values())
        assert len(distinct_hashes) == 1, (
            f"{fixture_name}: rollup output differed across hash seeds: {hashes}"
        )
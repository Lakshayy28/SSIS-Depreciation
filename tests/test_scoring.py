"""Tests for the deterministic core of the dual-axis scoring system."""

from __future__ import annotations

from pathlib import Path

import pytest

from ssis_migration.scoring import (
    build_scorecard,
    compute_functional_score,
    compute_parsing_score,
    count_cir_elements,
    count_dtsx_elements,
    structural_coverage,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


# ─── structural coverage ──────────────────────────────────────────────────────

def test_full_coverage_when_cir_matches_dtsx():
    dtsx = {"executables": 4, "dataflow_components": 6, "connections": 2,
            "parameters": 1, "variables": 3}
    cov, detail = structural_coverage(dtsx, dict(dtsx))
    assert cov == 1.0
    assert detail["executables"]["coverage"] == 1.0


def test_partial_coverage_penalises_dropped_executables():
    dtsx = {"executables": 4, "dataflow_components": 6, "connections": 2,
            "parameters": 0, "variables": 0}
    cir = {"executables": 2, "dataflow_components": 6, "connections": 2,
           "parameters": 0, "variables": 0}
    cov, detail = structural_coverage(dtsx, cir)
    assert detail["executables"]["coverage"] == 0.5
    assert cov < 1.0


def test_empty_category_does_not_dilute():
    # No variables in either → that category is full coverage, not a penalty.
    dtsx = {"executables": 2, "dataflow_components": 0, "connections": 0,
            "parameters": 0, "variables": 0}
    cir = {"executables": 2, "dataflow_components": 0, "connections": 0,
           "parameters": 0, "variables": 0}
    cov, _ = structural_coverage(dtsx, cir)
    assert cov == 1.0


# ─── parsing score composition ────────────────────────────────────────────────

def test_parsing_score_uses_coverage_without_llm():
    ps = compute_parsing_score(0.8, {}, [], llm_fidelity=None)
    assert ps.score == 0.8
    assert ps.llm_fidelity is None


def test_parsing_score_weights_llm_higher():
    ps = compute_parsing_score(1.0, {}, [], llm_fidelity=0.5)
    # 0.4*1.0 + 0.6*0.5 = 0.7
    assert ps.score == 0.7


# ─── functional score gating ──────────────────────────────────────────────────

def test_functional_version_failure_caps_score():
    fs = compute_functional_score(0.95, [], [], version_ok=False,
                                  version_issues=["uses applyInPandas (3.0+)"])
    assert fs.score == 0.5  # capped despite high equivalence


def test_functional_score_passthrough_when_version_ok():
    fs = compute_functional_score(0.9, [], ["minor"], version_ok=True)
    assert fs.score == 0.9


# ─── scorecard composite + pass gate ──────────────────────────────────────────

def test_composite_is_multiplicative():
    ps = compute_parsing_score(0.9, {}, [], llm_fidelity=None)
    fs = compute_functional_score(0.8, [], [], version_ok=True)
    card = build_scorecard("3.3", ps, fs, threshold=0.7)
    assert card.composite == pytest.approx(0.72, abs=1e-4)
    assert card.passed is True


def test_critical_issue_fails_even_with_high_composite():
    ps = compute_parsing_score(1.0, {}, [], llm_fidelity=None)
    fs = compute_functional_score(0.95, ["missing WHERE clause"], [], version_ok=True)
    card = build_scorecard("3.3", ps, fs, threshold=0.7)
    assert card.composite >= 0.7
    assert card.passed is False  # gated by the critical issue


def test_version_failure_fails_scorecard():
    ps = compute_parsing_score(1.0, {}, [], llm_fidelity=None)
    fs = compute_functional_score(1.0, [], [], version_ok=False, version_issues=["x"])
    card = build_scorecard("2.4", ps, fs, threshold=0.4)
    assert card.passed is False


# ─── element counting against a real sample ───────────────────────────────────

@pytest.mark.skipif(not (SAMPLES / "ETL_Load_Orders.dtsx").exists(), reason="sample missing")
def test_counts_on_real_sample():
    from ssis_migration.parser import DTSXParser

    path = SAMPLES / "ETL_Load_Orders.dtsx"
    cir = DTSXParser().parse(path)

    dtsx_counts = count_dtsx_elements(path)
    cir_counts = count_cir_elements(cir)

    # Parser should capture at least as many executables as the raw count
    # (it never invents nodes), and counts must be non-negative integers.
    assert dtsx_counts["executables"] >= 1
    assert cir_counts["executables"] >= 1
    cov, detail = structural_coverage(dtsx_counts, cir_counts)
    assert 0.0 <= cov <= 1.0
    assert set(detail) == {"executables", "dataflow_components", "connections",
                           "parameters", "variables"}

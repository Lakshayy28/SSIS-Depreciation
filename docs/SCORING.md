# Scoring — Dual-Axis Migration Fidelity

A single "did it work" number hides *where* fidelity was lost. Migration crosses
two lossy translations, so we score each independently and combine them.

```
PARSING fidelity        FUNCTIONAL equivalence
   DTSX → CIR              CIR / DTSX → PySpark
       │                          │
       └────────►  composite  ◄───┘
            = parsing × functional
```

Implemented in [`ssis_migration/scoring.py`](../ssis_migration/scoring.py); the
deterministic parts are pure and unit-tested, the judgments use the LLM as a
judge.

---

## Axis 1 — Parsing fidelity (DTSX → CIR)

> *Did the canonical representation capture everything in the `.dtsx`?*

**Deterministic — structural coverage.** Count the structurally significant SSIS
XML elements in the raw `.dtsx` and compare to what the CIR captured:

| Category | Weight |
|----------|--------|
| executables | 3.0 |
| data-flow components | 3.0 |
| connections | 1.0 |
| parameters | 1.0 |
| variables | 1.0 |

Per category, `coverage = min(cir, dtsx) / dtsx` (empty categories score 1.0 so
they don't dilute). The overall coverage is weighted by category weight × size,
so behaviour-bearing categories dominate.

**LLM audit — fidelity.** The `ParsingFidelityAgent` compares the raw DTSX
excerpt against the CIR summary and reports `missing_elements` and
`misrepresentations` with a `fidelity_score`. It catches *semantic* drops that
element counts miss (e.g. a captured component with the wrong join type).

**Combination:**

```
parsing.score = 0.4 · structural_coverage + 0.6 · llm_fidelity      (LLM ran)
parsing.score = structural_coverage                                 (no token)
```

The LLM is weighted higher because semantic capture matters more than raw counts.

---

## Axis 2 — Functional equivalence (CIR / DTSX → PySpark)

> *Does the PySpark do what the SSIS package did, on the target runtime?*

**LLM-as-judge — equivalence.** The 3-way `FunctionalValidatorAgent` reviews the
DTSX (ground truth), CIR, and PySpark together and returns an
`equivalence_score`, `critical_issues`, `warnings`, and `version_issues`.

**Version gate (deterministic + LLM).** `check_pyspark_version()` flags any API
that postdates the configured `SPARK_VERSION` (e.g. `applyInPandas` on 2.4). A
program that can't run on the target version **cannot** be equivalent on it, so
a version failure **hard-caps the functional score at 0.5**:

```
functional.score = equivalence
functional.score = min(equivalence, 0.5)     if version invalid
```

---

## Composite + pass gate

```
composite = parsing.score × functional.score        ∈ [0, 1]
```

Multiplicative on purpose: if parsing dropped half the package, even perfect
codegen of what remains is only half-equivalent to the original. End-to-end
fidelity is the product of the two stages.

A migration **passes** only when **all** hold:

1. `composite ≥ MIGRATION_PASS_THRESHOLD` (default `0.75`)
2. no functional **critical issues**
3. PySpark **version is valid**

Conditions 2–3 are independent gates: a high composite with a missing `WHERE`
clause or an invalid API still fails.

---

## Example scorecard (`scorecard_<pkg>.json`)

```json
{
  "spark_version": "2.4.8",
  "composite": 0.0881,
  "passed": false,
  "threshold": 0.75,
  "parsing": {
    "score": 0.5875,
    "structural_coverage": 0.8387,
    "llm_fidelity": 0.42,
    "element_detail": {
      "connections": { "dtsx": 6, "cir": 1, "coverage": 0.1667 }
    },
    "issues": ["Connection manager OLEDB_Source … not captured"]
  },
  "functional": {
    "score": 0.15,
    "equivalence": 0.15,
    "version_ok": false,
    "critical_issues": ["DFT Load Orders data flow completely missing"],
    "version_issues": ["pyodbc used; PySpark 2.4.8 cannot execute it inline"]
  }
}
```

This is the scoring system doing its job: it pinpoints *exactly* where fidelity
was lost (the parser caught 1 of 6 connections; codegen omitted a data flow;
the code targets the wrong runtime) instead of a vague pass/fail.

---

## Tuning

| Setting | Default | Effect |
|---------|---------|--------|
| `MIGRATION_PASS_THRESHOLD` | `0.75` | composite required to pass |
| `FUNCTIONAL_VALIDATION_MAX_ITERATIONS` | `2` | outer review→reconvert passes |
| `COPILOT_MAX_REVIEW_ITERATIONS` | `4` | inner component review→regen passes |
| `COPILOT_REVIEWER_MODEL` | `claude-haiku-4.5` | the judge model |

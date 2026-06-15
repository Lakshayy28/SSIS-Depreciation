# PySpark Target Version Analysis

## Recommendation: PySpark 3.3+

The original specification targets PySpark 2.4.8, which is five years past its last update (September 2021).

### Why 3.3+ is the right choice

| Feature | 2.4.8 | 3.3+ |
|---------|-------|------|
| Adaptive Query Execution (AQE) | ✗ | ✓ |
| Dynamic partition pruning | ✗ | ✓ |
| Pandas API on Spark | ✗ | ✓ (3.2+) |
| `spark.sql.legacy.*` flags for SQL Server compat | ✗ | ✓ |
| Python 3.10+ support | ✗ | ✓ |
| `SparkSession.builder.getOrCreate()` config-update bug | Present | Fixed (3.0) |
| `F.transform()`, `F.aggregate()`, higher-order functions | ✗ | ✓ |
| `DataFrame.mapInPandas()` / `applyInPandas()` | ✗ | ✓ |

### If 2.4.8 is truly required

The code generator must avoid: `F.transform()`, `F.aggregate()`, `F.forall()`, `F.exists()`,
`DataFrame.mapInPandas()`, `DataFrame.applyInPandas()`, `spark.sql.ansi.enabled`.

A `--compat-spark 2.4` flag in the CLI will constrain template selection accordingly.

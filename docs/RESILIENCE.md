# Resilience — NFRs for the LLM-backed pipeline

A package can trigger many Copilot calls (generation + inner review×N + outer
equivalence review + parsing audit), and a batch run multiplies that by hundreds
of packages. A bad token, a regional outage, or an aggressive rate limit must
not turn into hundreds of slow, doomed retries. Three composable primitives in
[`ssis_migration/resilience.py`](../ssis_migration/resilience.py) guard every
call; all are thread-safe.

```
 caller ─► [rate limiter] ─► [circuit breaker] ─► [retry w/ jitter] ─► Copilot API
              wait              fast-fail             bounded backoff
            for token          when OPEN          on 429 / 5xx / transport
```

---

## 1. Circuit breaker

`CircuitBreaker` is a classic three-state breaker, **registered process-wide by
name** so every `CopilotClient` the pipeline builds shares one breaker.

```
 CLOSED ──(N consecutive failures)──► OPEN ──(cooldown elapsed)──► HALF_OPEN
   ▲                                                                  │
   └──────────────── success ◄── probe ──► failure ──────────────────┘
                                                          (back to OPEN)
```

- **CLOSED** — normal operation.
- **OPEN** — every call fast-fails with `CircuitBreakerError` (surfaced as
  `CopilotUnavailableError`) for `COPILOT_CIRCUIT_BREAKER_COOLDOWN` seconds. This
  is the key win: one dead token trips the breaker once, and the rest of a batch
  run fails instantly instead of slow-retrying on every package.
- **HALF_OPEN** — after cooldown, a single probe is allowed; success closes the
  breaker, failure re-opens it.

A **4xx** (e.g. an invalid model id) is a caller/config error: it fails fast and
does **not** trip the breaker — only transport errors, 429, and 5xx count as
breaker failures.

| Setting | Default | Meaning |
|---------|---------|---------|
| `COPILOT_CIRCUIT_BREAKER_THRESHOLD` | `5` | consecutive failures before OPEN |
| `COPILOT_CIRCUIT_BREAKER_COOLDOWN` | `30` | seconds OPEN before a probe |

---

## 2. Retry with jittered backoff

`retry_call` / `backoff_delay` implement bounded exponential backoff with **full
jitter** (`random(0, base·2^attempt)`, capped). Jitter spreads retries so a
fleet of workers doesn't synchronise into a thundering herd. The Copilot client
retries transport errors, `429` (honouring `Retry-After`), and `5xx`.

| Setting | Default | Meaning |
|---------|---------|---------|
| `COPILOT_MAX_RETRIES` | `3` | attempts per call (4xx never retried) |
| `COPILOT_REQUEST_TIMEOUT` | `120` | per-request seconds |

---

## 3. Rate limiter

`TokenBucket` is an optional client-side limiter (refills at `rate` tokens/sec up
to a small burst capacity). Off by default; set a positive value to stay under a
seat's requests/minute budget without relying on the API returning 429s.

| Setting | Default | Meaning |
|---------|---------|---------|
| `COPILOT_RATE_LIMIT_PER_MIN` | `0` (off) | client-side requests/minute |

---

## Failure semantics

| Condition | Behaviour |
|-----------|-----------|
| Transient (timeout / 5xx / 429) | retried with jittered backoff |
| Retries exhausted | `CopilotUnavailableError`; breaker records failure |
| Breaker OPEN | immediate `CopilotUnavailableError`, no network call |
| 4xx (bad request/model) | immediate `RuntimeError`; breaker untouched |
| LLM phase raises | pipeline logs and **degrades gracefully** — deterministic output is still produced; scoring notes the missing judgment |

The pipeline treats the LLM as *augmentation*: if Copilot is unreachable, you
still get deterministic PySpark and a parsing-only assessment rather than a hard
failure.

All three primitives are unit-tested in
[`tests/test_resilience.py`](../tests/test_resilience.py).

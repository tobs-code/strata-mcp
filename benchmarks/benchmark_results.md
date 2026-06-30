
## Benchmark Run: 2026-06-30 18:56:02

| Tool | Avg (ms) | P95 (ms) | Min (ms) | Max (ms) | Errors |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `memory_stats` | 774.55 | 798.30 | 757.14 | 798.30 | 0 |
| `memory_store` | 790.20 | 814.75 | 762.20 | 814.75 | 0 |
| `memory_query` | 368.40 | 412.81 | 330.69 | 412.81 | 0 |
| `semantic_search` | 854.12 | 900.21 | 821.50 | 900.21 | 0 |
| `event_log_search` | 770.59 | 811.56 | 749.43 | 811.56 | 0 |
| `kg_query` | 766.28 | 787.76 | 749.55 | 787.76 | 0 |
| `explain_routing` | 0.20 | 0.22 | 0.19 | 0.22 | 0 |

---

## Benchmark Run: 2026-06-30 19:42:32

| Tool | Avg (ms) | P95 (ms) | Min (ms) | Max (ms) | Errors |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `memory_stats` | 851.77 | 1122.77 | 760.13 | 1122.77 | 0 |
| `memory_store` | 786.93 | 823.22 | 756.19 | 823.22 | 0 |
| `memory_query` | 362.01 | 400.54 | 323.91 | 400.54 | 0 |
| `semantic_search` | 806.62 | 870.25 | 782.46 | 870.25 | 0 |
| `event_log_search` | 783.67 | 822.17 | 761.45 | 822.17 | 0 |
| `kg_query` | 765.23 | 798.41 | 747.11 | 798.41 | 0 |
| `explain_routing` | 0.20 | 0.21 | 0.18 | 0.21 | 0 |

---

## Benchmark Run: 2026-06-30 19:44:02

### Summary

| Tool | Avg (ms) | P95 (ms) | Min (ms) | Max (ms) | Errors |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `memory_stats` | 784.86 | 831.33 | 761.31 | 831.33 | 0 |
| `memory_store` | 792.60 | 828.55 | 767.54 | 828.55 | 0 |
| `memory_query` | 391.88 | 429.05 | 334.11 | 429.05 | 0 |
| `semantic_search` | 809.22 | 892.39 | 781.75 | 892.39 | 0 |
| `event_log_search` | 795.02 | 828.39 | 770.72 | 828.39 | 0 |
| `kg_query` | 878.98 | 1028.94 | 787.49 | 1028.94 | 0 |
| `explain_routing` | 0.24 | 0.37 | 0.19 | 0.37 | 0 |

### Full Execution Logs

```text
STRATA MCP Performance Benchmark - 2026-06-30 19:44:02
============================================================
System: Windows 10 (AMD64)
Python: 3.11.9
============================================================

--- Benchmarking 'memory_stats' (10 iterations) ---
  Avg: 784.86ms
  P95: 831.33ms
  Min/Max: 761.31ms / 831.33ms
  Errors: 0

--- Benchmarking 'memory_store' (10 iterations) ---
[INFO] Initializing Embedding Service with model: sentence-transformers/all-MiniLM-L6-v2...
[OK] Embedding Service initialized
  Avg: 792.60ms
  P95: 828.55ms
  Min/Max: 767.54ms / 828.55ms
  Errors: 0

--- Benchmarking 'memory_query' (10 iterations) ---
  Avg: 391.88ms
  P95: 429.05ms
  Min/Max: 334.11ms / 429.05ms
  Errors: 0

--- Benchmarking 'semantic_search' (10 iterations) ---
  Avg: 809.22ms
  P95: 892.39ms
  Min/Max: 781.75ms / 892.39ms
  Errors: 0

--- Benchmarking 'event_log_search' (10 iterations) ---
  Avg: 795.02ms
  P95: 828.39ms
  Min/Max: 770.72ms / 828.39ms
  Errors: 0

--- Benchmarking 'kg_query' (10 iterations) ---
  Avg: 878.98ms
  P95: 1028.94ms
  Min/Max: 787.49ms / 1028.94ms
  Errors: 0

--- Benchmarking 'explain_routing' (10 iterations) ---
  Avg: 0.24ms
  P95: 0.37ms
  Min/Max: 0.19ms / 0.37ms
  Errors: 0

============================================================
Tool                 |   Avg (ms) |   P95 (ms) | Errors
------------------------------------------------------------
memory_stats         |     784.86 |     831.33 |      0
memory_store         |     792.60 |     828.55 |      0
memory_query         |     391.88 |     429.05 |      0
semantic_search      |     809.22 |     892.39 |      0
event_log_search     |     795.02 |     828.39 |      0
kg_query             |     878.98 |    1028.94 |      0
explain_routing      |       0.24 |       0.37 |      0
============================================================
```

---

## Benchmark Run: 2026-06-30 19:50:03

### Summary

| Tool | Avg (ms) | P95 (ms) | Min (ms) | Max (ms) | Errors |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `memory_stats` | 790.87 | 863.10 | 762.12 | 863.10 | 0 |
| `memory_store` | 796.96 | 818.69 | 785.12 | 818.69 | 0 |
| `memory_query` | 355.10 | 399.04 | 319.16 | 399.04 | 0 |
| `semantic_search` | 807.81 | 844.35 | 786.09 | 844.35 | 0 |
| `event_log_search` | 775.61 | 799.04 | 764.14 | 799.04 | 0 |
| `kg_query` | 772.53 | 813.60 | 752.64 | 813.60 | 0 |
| `explain_routing` | 0.20 | 0.32 | 0.18 | 0.32 | 0 |

### Full Execution Logs

```text
STRATA MCP Performance Benchmark - 2026-06-30 19:50:03
============================================================
System: Windows 10 (AMD64)
Python: 3.11.9
============================================================

--- Benchmarking 'memory_stats' (10 iterations) ---
  Avg: 790.87ms
  P95: 863.10ms
  Min/Max: 762.12ms / 863.10ms
  Errors: 0

--- Benchmarking 'memory_store' (10 iterations) ---
[INFO] Initializing Embedding Service with model: sentence-transformers/all-MiniLM-L6-v2...
[OK] Embedding Service initialized
  Avg: 796.96ms
  P95: 818.69ms
  Min/Max: 785.12ms / 818.69ms
  Errors: 0

--- Benchmarking 'memory_query' (10 iterations) ---
  Avg: 355.10ms
  P95: 399.04ms
  Min/Max: 319.16ms / 399.04ms
  Errors: 0

--- Benchmarking 'semantic_search' (10 iterations) ---
  Avg: 807.81ms
  P95: 844.35ms
  Min/Max: 786.09ms / 844.35ms
  Errors: 0

--- Benchmarking 'event_log_search' (10 iterations) ---
  Avg: 775.61ms
  P95: 799.04ms
  Min/Max: 764.14ms / 799.04ms
  Errors: 0

--- Benchmarking 'kg_query' (10 iterations) ---
  Avg: 772.53ms
  P95: 813.60ms
  Min/Max: 752.64ms / 813.60ms
  Errors: 0

--- Benchmarking 'explain_routing' (10 iterations) ---
  Avg: 0.20ms
  P95: 0.32ms
  Min/Max: 0.18ms / 0.32ms
  Errors: 0

============================================================
Tool                 |   Avg (ms) |   P95 (ms) | Errors
------------------------------------------------------------
memory_stats         |     790.87 |     863.10 |      0
memory_store         |     796.96 |     818.69 |      0
memory_query         |     355.10 |     399.04 |      0
semantic_search      |     807.81 |     844.35 |      0
event_log_search     |     775.61 |     799.04 |      0
kg_query             |     772.53 |     813.60 |      0
explain_routing      |       0.20 |       0.32 |      0
============================================================
```

---

## Benchmark Run: 2026-06-30 20:59:24

### Summary

| Tool | Avg (ms) | P95 (ms) | Min (ms) | Max (ms) | Errors |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `memory_stats` | 787.03 | 857.40 | 763.76 | 857.40 | 0 |
| `memory_store` | 768.65 | 789.28 | 753.44 | 789.28 | 0 |
| `memory_query` | 363.10 | 422.31 | 339.50 | 422.31 | 0 |
| `semantic_search` | 789.75 | 835.28 | 769.87 | 835.28 | 0 |
| `event_log_search` | 766.15 | 807.32 | 749.67 | 807.32 | 0 |
| `kg_query` | 759.73 | 799.96 | 740.39 | 799.96 | 0 |
| `explain_routing` | 0.20 | 0.35 | 0.18 | 0.35 | 0 |

### Full Execution Logs

```text
STRATA MCP Performance Benchmark - 2026-06-30 20:59:24
============================================================
System: Windows 10 (AMD64)
Python: 3.11.9
============================================================

--- Benchmarking 'memory_stats' (10 iterations) ---
[INFO] Starting background SurrealDB reconnect task
  Avg: 787.03ms
  P95: 857.40ms
  Min/Max: 763.76ms / 857.40ms
  Errors: 0

--- Benchmarking 'memory_store' (10 iterations) ---
[INFO] Initializing Embedding Service with model: sentence-transformers/all-MiniLM-L6-v2...
[OK] Embedding Service initialized
  Avg: 768.65ms
  P95: 789.28ms
  Min/Max: 753.44ms / 789.28ms
  Errors: 0

--- Benchmarking 'memory_query' (10 iterations) ---
  Avg: 363.10ms
  P95: 422.31ms
  Min/Max: 339.50ms / 422.31ms
  Errors: 0

--- Benchmarking 'semantic_search' (10 iterations) ---
  Avg: 789.75ms
  P95: 835.28ms
  Min/Max: 769.87ms / 835.28ms
  Errors: 0

--- Benchmarking 'event_log_search' (10 iterations) ---
  Avg: 766.15ms
  P95: 807.32ms
  Min/Max: 749.67ms / 807.32ms
  Errors: 0

--- Benchmarking 'kg_query' (10 iterations) ---
  Avg: 759.73ms
  P95: 799.96ms
  Min/Max: 740.39ms / 799.96ms
  Errors: 0

--- Benchmarking 'explain_routing' (10 iterations) ---
  Avg: 0.20ms
  P95: 0.35ms
  Min/Max: 0.18ms / 0.35ms
  Errors: 0

============================================================
Tool                 |   Avg (ms) |   P95 (ms) | Errors
------------------------------------------------------------
memory_stats         |     787.03 |     857.40 |      0
memory_store         |     768.65 |     789.28 |      0
memory_query         |     363.10 |     422.31 |      0
semantic_search      |     789.75 |     835.28 |      0
event_log_search     |     766.15 |     807.32 |      0
kg_query             |     759.73 |     799.96 |      0
explain_routing      |       0.20 |       0.35 |      0
============================================================
```

---

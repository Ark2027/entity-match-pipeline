## Deterministic baseline (v1)

- records evaluated: **92** (84 expecting a match, 8 expecting none)
- automated: **80**, deferred to a human: **8**
- automation precision: **100.0%** — of what it decided, how much was right
- automation rate: **95.2%** — of what should have matched, how much it handled
- **error rate: 0.0%** — automated decisions that were wrong
- trap survival: **100.0%** — records with no correct answer that were not resolved anyway

### Outcomes

| outcome | n |
|---|---|
| resolved_correct | 80 |
| deferred_correct | 4 |
| correctly_abstained | 4 |
| deferred_trap | 4 |

### By case type

| case                   | correctly_abstained | deferred_correct | deferred_trap | resolved_correct |
|------------------------|---------------------|------------------|---------------|------------------|
| ambiguous_abbreviation | 0                   | 4                | 0             | 0                |
| ambiguous_misspelling  | 0                   | 0                | 0             | 4                |
| ambiguous_rename       | 0                   | 0                | 0             | 4                |
| ambiguous_sibling      | 0                   | 0                | 0             | 4                |
| clean                  | 0                   | 0                | 0             | 48               |
| cross_state            | 0                   | 0                | 0             | 4                |
| dba                    | 0                   | 0                | 0             | 4                |
| duplicate              | 0                   | 0                | 0             | 4                |
| legal_suffix           | 0                   | 0                | 0             | 4                |
| no_counterpart         | 0                   | 0                | 4             | 0                |
| stopword_trap          | 4                   | 0                | 0             | 0                |
| transposed             | 0                   | 0                | 0             | 4                |


## With LLM adjudication (v2)

- records evaluated: **92** (84 expecting a match, 8 expecting none)
- automated: **84**, deferred to a human: **4**
- automation precision: **100.0%** — of what it decided, how much was right
- automation rate: **100.0%** — of what should have matched, how much it handled
- **error rate: 0.0%** — automated decisions that were wrong
- trap survival: **100.0%** — records with no correct answer that were not resolved anyway

### Outcomes

| outcome | n |
|---|---|
| resolved_correct | 84 |
| correctly_abstained | 4 |
| deferred_trap | 4 |

### By case type

| case                   | correctly_abstained | deferred_trap | resolved_correct |
|------------------------|---------------------|---------------|------------------|
| ambiguous_abbreviation | 0                   | 0             | 4                |
| ambiguous_misspelling  | 0                   | 0             | 4                |
| ambiguous_rename       | 0                   | 0             | 4                |
| ambiguous_sibling      | 0                   | 0             | 4                |
| clean                  | 0                   | 0             | 48               |
| cross_state            | 0                   | 0             | 4                |
| dba                    | 0                   | 0             | 4                |
| duplicate              | 0                   | 0             | 4                |
| legal_suffix           | 0                   | 0             | 4                |
| no_counterpart         | 0                   | 4             | 0                |
| stopword_trap          | 4                   | 0             | 0                |
| transposed             | 0                   | 0             | 4                |


## Cost and latency

- adjudications: **8** (resolved 4, declined 4, errors 0)
- tokens: 12,110 in / 1,408 out
- latency: 2,519 ms average

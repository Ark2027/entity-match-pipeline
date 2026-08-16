## Deterministic baseline (v1)

- records evaluated: **92** (84 expecting a match, 8 expecting none)
- automated: **71**, deferred to a human: **17**
- automation precision: **100.0%** — of what it decided, how much was right
- automation rate: **84.5%** — of what should have matched, how much it handled
- **error rate: 0.0%** — automated decisions that were wrong
- trap survival: **100.0%** — records with no correct answer that were not resolved anyway

### Outcomes

| outcome | n |
|---|---|
| resolved_correct | 71 |
| deferred_correct | 11 |
| correctly_abstained | 4 |
| deferred_trap | 4 |
| deferred_wrong | 2 |

### By case type

| case                   | correctly_abstained | deferred_correct | deferred_trap | deferred_wrong | resolved_correct |
|------------------------|---------------------|------------------|---------------|----------------|------------------|
| ambiguous_abbreviation | 0                   | 4                | 0             | 0              | 0                |
| ambiguous_misspelling  | 0                   | 0                | 0             | 0              | 4                |
| ambiguous_rename       | 0                   | 0                | 0             | 0              | 4                |
| ambiguous_sibling      | 0                   | 0                | 0             | 0              | 4                |
| clean                  | 0                   | 7                | 0             | 2              | 39               |
| cross_state            | 0                   | 0                | 0             | 0              | 4                |
| dba                    | 0                   | 0                | 0             | 0              | 4                |
| duplicate              | 0                   | 0                | 0             | 0              | 4                |
| legal_suffix           | 0                   | 0                | 0             | 0              | 4                |
| no_counterpart         | 0                   | 0                | 4             | 0              | 0                |
| stopword_trap          | 4                   | 0                | 0             | 0              | 0                |
| transposed             | 0                   | 0                | 0             | 0              | 4                |


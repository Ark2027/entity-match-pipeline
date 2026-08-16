# Entity Match Pipeline

Reconciles business records between a spreadsheet and a CRM when the two have no key in common.

The situation it was built for: partner organizations submit quarterly workbooks of loans they originated, and those same businesses may or may not exist in our CRM as leads we sent them. Nobody shares an ID. The names don't match — one side has `Harbor Point Marine, L.L.C.`, the other has `Harbor Point Marine LLC`, and sometimes the CRM only knows the trading name from a DBA. Reconciling a quarter by hand took about a day, and the answers weren't reproducible.

This does it in about a second, and shows its working.

## Try it without setting anything up

No database, no credentials, no API keys. The bundled fixtures are fictional.

```bash
pip install -e .
python scripts/generate_fixtures.py
python run_matcher.py --config config/settings.demo.json --quarter 2026Q2
```

That produces `output/match_review_current.xlsx`. From the fixtures as committed:

```
live rows in quarter        88
collapsed review items      82
CRM candidates             104
auto-accepted               66
queued for review            8
exceptions                  12
duplicates collapsed         6
```

The 8 in review are the interesting ones. Each scored 100 on name similarity but had a runner-up within 5 points, so the pipeline declined to pick and queued them instead.

## How it decides

**Block first.** Candidates are grouped by source and state, so a business in Oregon is never compared against one in Wisconsin. This is a hard filter, not a scoring penalty — it makes the search tractable and removes a whole class of wrong answers up front.

**Build name variants.** Each name expands into several comparable forms: accents folded, punctuation removed, legal suffix stripped, DBA split into both sides, generic words dropped. `Kestrel Holdings LLC dba Kestrel Coffee Roasters` produces variants for both the holding company and the coffee roaster, because the CRM might know either.

**Score, then corroborate.** `rapidfuzz` on the best variant pair gives the name score. Then supporting signals adjust it: matching postal code adds 4, an amount that falls inside the candidate's stated size band adds 3 (or 1 if it's merely close), and lead date ordering is recorded as a reason. Every match carries the list of signals that produced it, so a reviewer can see *why* rather than trusting a number.

**Band the result.** Auto-accept, review, possible match, or discard. To be auto-accepted a pair must clear the score threshold **and** beat the runner-up by a configured gap. That second condition is what stops confident wrong answers.

## The case this is really built around

Two names can score well on raw similarity while having nothing in common:

```
Capital Investment Group   vs   Capital Ventures Group
```

Every shared word is a stopword. Strip them and you're comparing `investment` against `ventures`, which are unrelated. Naive fuzzy matching accepts this pair. `_has_meaningful_name_signal()` rejects it, and in the demo run that pair correctly ends up unmatched rather than wrong.

Most of the code is not about matching things. It's about declining to.

## Hard cases in the fixtures

The generator seeds these deliberately, and the demo run resolves all of them correctly:

| Case | Expected | Result |
|---|---|---|
| `Harbor Point Marine, L.L.C.` vs `... LLC` | match | auto-accepted |
| `Kestrel Holdings LLC dba Kestrel Coffee Roasters` | match the trading name | auto-accepted |
| `Ironwood Custom Cabinetry` vs `Custom Cabinetry Ironwood` | match | auto-accepted |
| Same business name in two states | match only the right state | correct state, gap 34+ |
| `Capital Investment Group` vs `Capital Ventures Group` | refuse | left unmatched |
| Row duplicated in the workbook | collapse to one | `duplicate_count=2` |
| `A100234` as a business name | reject | exception |
| Origination outside the quarter | exclude | absent |

## State between runs

Runs are idempotent. Matches are recorded in SQLite with a stable key, so the second run distinguishes new candidates from ones already surfaced, and a reviewer's decisions survive re-running. Re-running an unchanged quarter produces the same queue, not a duplicate one.

## Configuration

Everything domain-specific lives in `config/settings.json`. `config/settings.example.json` is the annotated template.

Thresholds are worth tuning to your data. `auto_accept_score` sets how good a match must be, `min_score_gap` sets how much better than the runner-up, and `max_review_rows` caps what lands in front of a human per run.

Source workbooks are identified by filename via `source_mappings` — a short code plus the markers that might appear in the filename, so `Q2-NORTHWEST-originations.xlsx` and `northwest_q2.xlsx` resolve to the same source.

## On data minimisation

The candidate query selects only columns the matcher actually consumes. That sounds obvious, but the version this was generalized from pulled ten more, including a partial national ID field that nothing downstream ever read. If a column isn't scored, joined on, or shown to a reviewer, it shouldn't leave the database.

Contact details are treated the same way. They aren't fetched, aren't stored in the match history, and `drop_sensitive_columns()` removes anything matching a personal-data pattern before a workbook is written. Hiding an Excel column is not redaction — it survives the file and one right-click reveals it.

## Tests

```bash
python tests/test_normalization.py
python tests/test_matching.py
```

51 tests, standard library only. They cover name normalization, DBA splitting, the stopword guard, threshold banding, corroboration scoring, duplicate collapsing, quarter maths, and output safety.

One of them earns its keep: `L.L.C.` used to normalize to three separate tokens `l l c`, which never matched the `llc` suffix entry. The end-to-end run hid it because a different variant happened to score well. The unit test didn't.

## Running against a real database

```bash
pip install -e ".[db]"
```

Candidates come over an SSH tunnel via `psql`. Two details in there that were fixed while generalizing this:

- The database password is written to the remote shell's stdin rather than interpolated into the command string, because a command string is visible in `ps` on the remote host while it runs.
- Host keys are verified against `known_hosts` and unknown ones are rejected. `AutoAddPolicy` trusts whatever answers, which defeats the point.

## Adding a language model to the tail

The scorer deliberately declines ambiguous pairs. That leaves a queue for a human, which is safe but not free. `llm_adjudicator.py` offers those deferrals to a model, which either picks a candidate or declines.

It only ever sees the deferred band. It cannot touch an auto-accept and cannot resurrect a discard, so it can add automation without being able to damage decisions that were already made. A returned id that was never offered is treated as a refusal, and low confidence stays deferred.

Measured against the same ground truth:

| | deterministic | with adjudication |
|---|---|---|
| automation rate | 95.2% | **100%** |
| automation precision | 100% | **100%** |
| error rate | 0% | **0%** |
| trap survival | 100% | **100%** |
| deferred to a human | 8 | **4** |

It resolved 4 of 8 deferrals, all correctly, and declined the other 4 — which were traps with no correct answer. About **$0.007 per adjudication**, on roughly 8% of records.

```bash
pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...
python eval/run_adjudication.py
```

The evaluation is the point rather than the model call, and it earned its keep by finding two bugs — a tool schema with two fields encoding one fact, which made every adjudication silently decline, and ground truth that contradicted itself, which reported a model error that was mine. Both are written up in [`eval/results.md`](eval/results.md), along with what I would not claim from a sample this small.

## Limits

Blocking on state means a business that moved between the workbook and the CRM won't match. That's a deliberate trade: it costs a few true positives to remove a lot of false ones.

Name matching is Latin-script and English-centric. The stopword list, legal suffixes and shorthand expansions all assume US business naming.

Everything is in-memory pandas. Fine for tens of thousands of rows on each side; it is not a distributed record linkage engine and doesn't try to be.

## License

MIT

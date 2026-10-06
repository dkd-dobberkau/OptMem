# OptMem eval

`test.py` checks the mechanics (tiling, budgets, locking, crash recovery) with a
fake compressor. It cannot tell whether the **summaries** are any good. This
does: it builds the whole summary tree with a real compressor and measures how
many concrete facts are still readable at every block size.

## Run

```sh
python3 eval/run.py --fake                      # truncation baseline, no LLM, ~1s
python3 eval/run.py --compressor 'claude -p'    # any command: prompt on stdin, one line on stdout
python3 eval/run.py --compressor '...' --variant strict --json strict.json
```

The compressor is any shell command. It receives the exact prompt `memo`
would show an agent (built by `nap_prompt()`, minus the final `Run:` line) on
stdin and prints the summary line. If it prints several lines, the last one
counts. A line over the byte limit gets one retry, then is cut, and both are
counted in the report, because an agent hits the same wall. `--jobs` sets how
many blocks of one level are compressed in parallel.

`--variant strict` swaps in the older, stricter instruction ("keep every name,
number, date, decision and outcome"), `--instruction "..."` any text of your
own. That is the A/B hook for prompt changes. `--json` also stores every
summary, so two runs can be diffed.

## What is measured

- **Fixture** (`fixtures/falkenstein.*`): 64 German project notes of a made-up
  agency relaunch, with trivia and one correction. The `.txt` is in `memo import`
  format.
- **Probes** (`*.probes.json`): 28 details (names, amounts, ticket ids), each
  living in exactly one note. A probe counts as kept if its regex matches the
  summary of the block that covers the note. This tests detail, not gist: a
  summary can keep "Hetzner" and lose "Nürnberg".
- **Wake view**: how many probes are readable in the `wake` document at several
  `WAKE_LINES` budgets, i.e. without `recall` or `zoom`.
- **Trivia** that should be dropped, and a **correction** (old date replaced by
  a new one) that should win.

Add your own fixture as `fixtures/<name>.txt` plus `<name>.probes.json` and run
with `--fixture <name>`.

## Reference run

Compressor: Claude subagents with the prompt exactly as `memo nap` prints it
(`current`), one run, 2026-10-06. Treat it as one data point, not a benchmark.

| block | facts kept | compression |
|---|---|---|
| raw | 28/28 | 1.0:1 |
| 2 | 27/28 | 1.2:1 |
| 4 | 26/28 | 1.5:1 |
| 8 | 25/28 | 3.0:1 |
| 16 | 19/28 | 5.9:1 |
| 32 | 10/28 | 12:1 |
| 64 | 5/28 | 22:1 |

The truncation baseline (`--fake`) keeps 6/28 at 16 and 3/28 at 64, so an LLM
compressor clearly helps, mostly in the middle levels. All 5 trivia notes were
dropped at every level, and the correction survived. Nothing in the fixture
reaches the 256:1 range the README plans for large logs.

## Limits

One fixture, one language, notes written by a model, regex-based scoring. Good
for comparing two prompts or two models on the same data, not for absolute
claims. Run-to-run variance is not measured yet: repeat a variant a few times
before believing a small difference.

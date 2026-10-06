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

For a clean, comparable run use the bundled compressor, a bare API call with
no agent, tools or local context (needs `pip install anthropic` and
`ANTHROPIC_API_KEY` in the environment; the model is `OPTMEM_EVAL_MODEL`,
default `claude-sonnet-5-5`):

```sh
python3 eval/run.py --compressor 'python3 eval/compressors/anthropic_api.py' --json current-1.json
```

It has only been tested against a stub SDK, not the live API.

The compressor is any shell command. It receives the exact prompt `memo`
would show an agent (built by `nap_prompt()`, minus the final `Run:` line) on
stdin and prints the summary line. If it prints several lines, the last one
counts. A line over the byte limit gets one retry, then is cut, and both are
counted in the report, because an agent hits the same wall. `--jobs` sets how
many blocks of one level are compressed in parallel.

`--variant state` is `strict` plus "if something was later solved, replaced or corrected, give only the final state" (see *stale status* below).
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
- **Stale status** (`stale` in the probes file): a problem note (Klarna ticket open, Lighthouse 72, 41.000 accounts) and the later note that resolves it. Counted as stale when a block that covers both still shows the problem but not the solution. Regex-based, so read the example summaries too.
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

### Prompt A/B with the API compressor

`eval/compressors/anthropic_api.py`, `claude-sonnet-5-5`, 2026-10-06. Facts kept
out of 28, one number per run (`state` ran twice, the others three times):

| block | `current` | `strict` | `state` |
|---|---|---|---|
| 2 | 28 28 28 | 28 27 28 | 28 28 |
| 4 | 26 26 25 | 26 26 26 | 26 27 |
| 8 | 22 21 20 | 26 27 26 | 25 24 |
| 16 | 16 18 17 | 19 20 19 | 18 20 |
| 32 | 9 11 11 | 11 14 9 | 13 10 |
| 64 | 4 6 8 | 8 10 7 | 7 7 |
| wake, 16 lines | 19 20 21 | 23 25 23 | 20 22 |
| wake, 8 lines | 14 15 14 | 16 21 17 | 17 15 |

Identical runs differ by up to 2-4 facts. What holds up: at blocks 8 and 16
and in the 16-line wake view, `strict` keeps about 3-6 more details than
`current`, and no run of one variant reaches a run of the other. At blocks 32
and 64 the ranges overlap, so there is no established difference. The price:
`strict` drops less trivia at the low levels (all 5 noise notes still present
at blocks 2 and 4, 2-3 at block 8, none from 32 up; `current` drops most of
them from block 4) and writes telegraphic summaries ("Mollie>PayPal",
"Konten 41.000→31.700").

`state` (`strict` plus "if something was later solved, replaced or corrected,
give only the final state") shows no consistent gain. It kept 1-3 fewer
details than `strict` and triggered 3-4 times as many too-long retries. The
stale-status probe (3 pairs) showed 0-1 stale at block 64 for every variant,
so it cannot separate them; one `state` run did write "MOL-55120 gelöst" at the
root, the other did not. Both variants keep the correction at the root. Three
runs, one fixture, one model: a lead, not a result.

**The compressor matters a lot, and a bad one invalidates the run.** Two runs
with `--compressor 'claude -p'` on another machine kept only 12-15/28 at block
size 2 and 0-1/28 from 8 up. They are not a measurement of the prompt: 52 of
65 responses had several lines, and the line taken as the summary was often a
side remark ("I left out ...", a connector notice, a leaked date from the
local context). `claude -p` is a full agent that loads the local context, so
it is not a bare model. Use `eval/compressors/anthropic_api.py` (below) or
check the "example summaries" before trusting any number (next section).

## When the numbers look wrong

Read the "example summaries" at the end of the report first. Then check:

- **Several lines per response.** If the report warns about it, the compressor
  adds a preamble or a trailing note, and the wrong line may have been taken as
  the summary. Look at the `raw` entries in the `--json` file, and try
  `--pick longest` (or `first`).
- **Errors that look like answers.** CLIs sometimes print rate-limit or auth
  messages on stdout with exit code 0; they would be stored as summaries.
- **An agent instead of a bare model.** Tools like `claude -p` run a full
  coding agent: it may load project files (`CLAUDE.md`, memory) or use tools,
  which changes what comes back. Prefer a setup without tools and project
  context (see the CLI's help for the flags), or call the API directly with a
  small script that reads the prompt on stdin.
- **A failing compressor** aborts the run and prints its stderr and stdout.

## Limits

One fixture, one language, notes written by a model, regex-based scoring. Good
for comparing two prompts or two models on the same data, not for absolute
claims. Run-to-run variance is not measured yet: repeat a variant a few times
before believing a small difference.

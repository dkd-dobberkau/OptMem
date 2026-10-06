#!/usr/bin/env python3
"""OptMem eval: which facts survive compression, level by level?

Loads a fixture (dated memories + probe facts), builds the whole summary tree
with a compressor of your choice, and reports how many probe facts are still
readable at every block size and in the `wake` document at several budgets.

  python3 eval/run.py --fake                       # no LLM: truncation baseline
  python3 eval/run.py --compressor 'claude -p'     # any command: prompt on stdin,
                                                   #   one line on stdout
  python3 eval/run.py --compressor '...' --variant strict --json out.json

The prompts are the real ones: they are built by memo's own nap_prompt(), so
this measures the tool as an agent would experience it. Only the final
"Run: memo nap ..." line is dropped, so nothing tries to execute it.
"""

import argparse
import concurrent.futures as cf
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)


def load_memo():
    loader = SourceFileLoader("memo_cli", os.path.join(ROOT, "memo"))
    spec = importlib.util.spec_from_loader("memo_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cli = load_memo()

# Instruction lines to swap into the nap prompt. `current` keeps whatever memo
# prints today; the others are candidates to compare against it.
VARIANTS = {
    "current": None,
    "strict": "Keep every name, number, date, decision and outcome. "
              "Drop wording, not facts. Invent nothing.",
}


# ---------------------------------------------------------------- fixture

def load_fixture(name):
    base = os.path.join(HERE, "fixtures", name)
    memories = []
    for n, line in enumerate(open(base + ".txt", encoding="utf-8"), 1):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        date, _, text = line.partition(" ")
        memories.append((date, text.strip()))
    spec = json.load(open(base + ".probes.json", encoding="utf-8"))
    return memories, spec


def quiet_check(text):
    """memo's own validation (one line, byte limit), without its sys.exit."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return cli.check(text), None
    except SystemExit:
        return None, err.getvalue().strip()


def make_store(memories):
    d = tempfile.mkdtemp(prefix="optmem-eval-")
    os.makedirs(os.path.join(d, "TREE"))
    open(os.path.join(d, "LOG.txt"), "ab").close()
    items = []
    for i, (date, text) in enumerate(memories):
        ok, err = quiet_check(text)
        if err:
            sys.exit("fixture memory %d is invalid: %s" % (i, err))
        items.append((date, ok))
    cli.log_append(d, items)
    return d


# ---------------------------------------------------------------- compressors

def cut(text, limit):
    b = text.encode()
    return text if len(b) <= limit else b[:limit].decode("utf-8", "ignore").rstrip()


def fake_compressor(prompt):
    """Baseline: join the memories and cut. A real compressor should beat it."""
    body = [re.sub(r"^\s*#[\d-]+ (\d{4}-\d{2}-\d{2} )?", "", l)
            for l in prompt.splitlines() if l.startswith("  #")]
    return " ".join(body)


def command_compressor(cmd):
    """The raw stdout of the command; picking the line happens in compress()."""
    def run(prompt):
        r = subprocess.run(cmd, shell=True, input=prompt, capture_output=True,
                           text=True, timeout=600)
        if r.returncode:
            # CLIs often report errors (rate limit, auth) on stdout, not stderr
            raise RuntimeError("compressor exited %d\n  stderr: %s\n  stdout: %s" % (
                r.returncode, r.stderr.strip()[:300] or "(empty)",
                r.stdout.strip()[:300] or "(empty)"))
        if not r.stdout.strip():
            raise RuntimeError("compressor printed nothing")
        return r.stdout
    return run


def pick_line(raw, how="last"):
    """The summary is one line of the response (default: the last non-empty
    one). Also report how many lines there were, because a preamble or a
    trailing note means the compressor is not answering with just the line;
    `--pick longest` is the fix for a trailing note."""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return "", 0
    line = {"last": lines[-1], "first": lines[0], "longest": max(lines, key=len)}[how]
    return " ".join(line.split()), len(lines)


def compress(fn, prompt, limit, retry=True, how="last"):
    """One block, as an agent would: a too-long line gets one retry, like the
    tool's own 'Too long' error would trigger."""
    raw = fn(prompt)
    line, nlines = pick_line(raw, how)
    res = {"raw": raw, "multiline": nlines > 1, "retried": False, "cut": False}
    if retry and len(line.encode()) > limit:
        res["retried"] = True
        raw2 = fn("%s\n\nYour last line was %d bytes. The limit is %d bytes. "
                  "Compress it further." % (prompt, len(line.encode()), limit))
        line, n2 = pick_line(raw2, how)
        res["raw_retry"] = raw2
        res["multiline"] = res["multiline"] or n2 > 1
    if len(line.encode()) > limit:
        res["cut"] = True
        line = cut(line, limit)
    res["line"] = line
    return res


def build_prompt(d, lo, hi, instruction):
    lines = [l for l in cli.nap_prompt(d, lo, hi, 0).splitlines()
             if not l.startswith("Run:")]
    assert lines[1].startswith("Keep"), "nap prompt layout changed: " + lines[1]
    if instruction:
        lines[1] = instruction
    return "\n".join(lines).strip()


def build_tree(d, n, fn, instruction, jobs, show_prompt, retry=True, how="last"):
    stats = {"calls": 0, "retried": 0, "truncated": 0, "multiline": 0}
    raws = {}  # block id -> what the compressor actually printed
    size = 2
    while size <= n:
        blocks = [(lo, lo + size) for lo in range(0, n - n % size, size)]
        prompts = [build_prompt(d, lo, hi, instruction) for lo, hi in blocks]
        if show_prompt and size == 2:
            print("--- example prompt (block %d-%d) ---\n%s\n---\n"
                  % (blocks[0][0], blocks[0][1] - 1, prompts[0]))
        with cf.ThreadPoolExecutor(jobs) as ex:
            results = list(ex.map(
                lambda p: compress(fn, p, cli.ENTRY_CHARS, retry, how), prompts))
        for (lo, hi), res in zip(blocks, results):
            ok, err = quiet_check(res["line"])
            if err:
                sys.exit("block %d-%d: %s\n  raw response: %r"
                         % (lo, hi - 1, err, res["raw"][:300]))
            assert cli.tree_put(d, lo, hi, ok)
            stats["calls"] += 1 + res["retried"]
            stats["retried"] += res["retried"]
            stats["truncated"] += res["cut"]
            stats["multiline"] += res["multiline"]
            raws["%d-%d" % (lo, hi - 1)] = {k: res[k] for k in ("raw", "raw_retry") if k in res}
        size *= 2
    return stats, raws


# ---------------------------------------------------------------- evaluation

def level_text(d, size, i):
    if size == 1:
        return cli.log_get(d, i)[2]
    lo = i // size * size
    return cli.tree_get(d, lo, lo + size) or ""


def evaluate(d, n, spec, widths):
    probes = spec["probes"]
    sizes = [1]
    while sizes[-1] * 2 <= n:
        sizes.append(sizes[-1] * 2)
    raw_avg = sum(len(level_text(d, 1, i).encode()) for i in range(n)) / n
    res = {"levels": [], "wake": [], "noise": [], "lost": []}
    hits = {}
    for s in sizes:
        reach = n // s * s  # memories covered by a complete block
        elig = [p for p in probes if p["id"] < reach]
        ok = {p["label"]: bool(re.search(p["regex"], level_text(d, s, p["id"]), re.I))
              for p in elig}
        hits[s] = ok
        blocks = [level_text(d, s, lo) for lo in range(0, reach, s)]
        avg = sum(len(b.encode()) for b in blocks) / len(blocks)
        res["levels"].append({"size": s, "survived": sum(ok.values()), "of": len(ok),
                              "avg_bytes": round(avg), "ratio": round(raw_avg * s / avg, 1)})
    for p in probes:
        gone = [s for s in sizes if p["label"] in hits[s] and not hits[s][p["label"]]]
        if gone:
            res["lost"].append({"id": p["id"], "label": p["label"], "first_lost": gone[0],
                                "missing_at": gone})
    for w in widths:
        if w >= n:
            continue
        cov = cli.cover(n, w)
        got = 0
        for p in probes:
            lo, hi = next(b for b in cov if b[0] <= p["id"] < b[1])
            txt = level_text(d, 1, p["id"]) if hi - lo == 1 else (cli.tree_get(d, lo, hi) or "")
            got += bool(re.search(p["regex"], txt, re.I))
        res["wake"].append({"wake_lines": w, "lines": len(cov), "survived": got,
                            "of": len(probes), "sizes": sorted({hi - lo for lo, hi in cov})})
    for s in sizes[1:]:
        reach = n // s * s
        noise = [x for x in spec.get("noise", []) if x["id"] < reach]
        leak = sum(bool(re.search(x["regex"], level_text(d, s, x["id"]), re.I)) for x in noise)
        res["noise"].append({"size": s, "leaked": leak, "of": len(noise)})
    c = spec.get("correction")
    if c:
        s = 2
        while s <= n and c["old_id"] // s != c["new_id"] // s:
            s *= 2
        if s <= n and n // s * s > c["new_id"]:
            t = level_text(d, s, c["new_id"])
            res["correction"] = {"size": s, "has_new": c["new"] in t, "has_old": c["old"] in t,
                                 "text": t}
    return res


def report(res, meta, summaries):
    print("compressor: %s | prompt: %s | memories: %d" % (meta["compressor"], meta["variant"], meta["n"]))
    st = meta["stats"]
    print("compressions: %d calls, %d retried (too long), %d hard-truncated"
          % (st["calls"], st["retried"], st["truncated"]))
    if st["multiline"]:
        print("WARNING: %d responses had several lines (the %s one was used). The "
              "compressor is not answering with just the line;\n"
              "         check the \"raw\" entries in the --json file, or change --pick."
              % (st["multiline"], meta["pick"]))
    print()
    print("%-8s %-14s %-9s %s" % ("block", "facts kept", "avg bytes", "compression"))
    for l in res["levels"]:
        print("%-8s %2d/%d (%3.0f%%)   %-9d %.1f:1" % (
            "raw" if l["size"] == 1 else l["size"], l["survived"], l["of"],
            100 * l["survived"] / l["of"], l["avg_bytes"], l["ratio"]))
    if res["lost"]:
        print("\nlost facts (first block size where the detail is gone):")
        for x in res["lost"]:
            print("  #%-3d %-34s %d" % (x["id"], x["label"], x["first_lost"]))
    print("\nwake document (facts readable without recall/zoom):")
    for w in res["wake"]:
        print("  WAKE_LINES=%-3d %3d lines, block sizes %-26s %d/%d" % (
            w["wake_lines"], w["lines"], w["sizes"], w["survived"], w["of"]))
    print("\ntrivia still present in summaries:")
    print("  " + ", ".join("%d: %d/%d" % (x["size"], x["leaked"], x["of"]) for x in res["noise"]))
    c = res.get("correction")
    if c:
        print("\ncorrection check (block %d covering old and new value): keeps new=%s, still shows old=%s"
              % (c["size"], c["has_new"], c["has_old"]))
    print("\nexample summaries (look at these before trusting the numbers):")
    n, size = meta["n"], 2
    while size <= n:
        key = "0-%d" % (size - 1)
        if key in summaries and size in (2, 8, 32, 128, 512) or size * 2 > n:
            print("  #%s [%d bytes] %s" % (key, len(summaries[key].encode()), summaries[key]))
        size *= 2


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--compressor", metavar="CMD",
                   help="shell command: prompt on stdin, one line on stdout")
    g.add_argument("--fake", action="store_true", help="truncation baseline, no LLM")
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="current",
                    help="instruction line in the nap prompt (default: whatever memo prints)")
    ap.add_argument("--instruction", help="custom instruction line, overrides --variant")
    ap.add_argument("--pick", choices=("last", "first", "longest"), default="last",
                    help="which line of a multi-line response is the summary (default: last)")
    ap.add_argument("--fixture", default="falkenstein")
    ap.add_argument("--jobs", type=int, default=4, help="parallel compressions per level")
    ap.add_argument("--widths", default="48,32,24,16,12,8",
                    help="WAKE_LINES budgets to evaluate")
    ap.add_argument("--json", metavar="PATH", help="write the full result incl. every summary")
    ap.add_argument("--show-prompt", action="store_true", help="print one example prompt")
    ap.add_argument("--keep", action="store_true", help="keep the temporary store, print its path")
    a = ap.parse_args()

    memories, spec = load_fixture(a.fixture)
    n = len(memories)
    instruction = a.instruction or VARIANTS[a.variant]
    d = make_store(memories)
    try:
        fn = fake_compressor if a.fake else command_compressor(a.compressor)
        try:
            stats, raws = build_tree(d, n, fn, instruction, a.jobs, a.show_prompt,
                                     retry=not a.fake, how=a.pick)
        except (RuntimeError, subprocess.SubprocessError) as e:
            sys.exit("compressor failed: %s" % e)
        res = evaluate(d, n, spec, [int(x) for x in a.widths.split(",") if x])
        summaries = {}
        s = 2
        while s <= n:
            for lo in range(0, n - n % s, s):
                summaries["%d-%d" % (lo, lo + s - 1)] = cli.tree_get(d, lo, lo + s)
            s *= 2
        meta = {"compressor": "fake (truncation)" if a.fake else a.compressor,
                "variant": "custom" if a.instruction else a.variant, "n": n,
                "fixture": a.fixture, "pick": a.pick, "stats": stats}
        report(res, meta, summaries)
        if a.json:
            json.dump({"meta": meta, "result": res, "summaries": summaries, "raw": raws},
                      open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print("\nwrote " + a.json)
        if a.keep:
            print("store kept at " + d)
            d = None
    finally:
        if d:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()

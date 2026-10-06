#!/usr/bin/env python3
"""Compressor for eval/run.py that calls the Anthropic API directly.

A bare model call: no agent, no tools, no project files, no connectors, no
memory of the person running it. That is what makes results comparable, which
`claude -p` is not (it loads the local context and adds side remarks).

  pip install anthropic
  export ANTHROPIC_API_KEY=...
  python3 eval/run.py --compressor 'python3 eval/compressors/anthropic_api.py'

The model comes from OPTMEM_EVAL_MODEL (default: claude-sonnet-5-5). Reads the
prompt on stdin, prints the summary line on stdout. Errors go to stderr with a
non-zero exit code, which run.py shows.
"""

import os
import sys

SYSTEM = (
    "You are the compression step of a memory tool. Follow the instruction in "
    "the user's message exactly. Answer with the one line it asks for and "
    "nothing else: no preamble, no notes, no quotation marks, no id prefix."
)


def main():
    try:
        import anthropic
    except ImportError:
        sys.exit("the anthropic package is missing: pip install anthropic")
    prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("empty prompt on stdin")
    model = os.environ.get("OPTMEM_EVAL_MODEL", "claude-sonnet-5-5")
    try:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        msg = client.messages.create(
            model=model,
            max_tokens=400,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # one readable line instead of a traceback
        sys.exit("API call failed (model %s): %s: %s" % (model, type(e).__name__, e))
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    if not text.strip():
        sys.exit("the model returned no text (stop_reason=%s)" % getattr(msg, "stop_reason", "?"))
    print(text.strip())


if __name__ == "__main__":
    main()

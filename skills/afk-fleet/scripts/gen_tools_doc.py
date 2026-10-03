#!/usr/bin/env python3
"""
gen_tools_doc.py — write each subcommand's signature into references/tools.md.

The interface table's first column is not written by hand: it is rendered here
from `afk.build_parser()`, so a flag added, renamed or made required shows up in
the table by running this, and a test fails while the table is stale. The other
columns — what a subcommand does, and its kind — are prose and stay hand-written.

  gen_tools_doc.py           rewrite references/tools.md in place
  gen_tools_doc.py --check   exit 1 if it would change anything
"""
import os
import re
import sys

import afk

TOOLS_MD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "references", "tools.md")

# The flags of the shared calling convention, which tools.md states once above the
# table instead of on every row.
SHARED = ("--help", "--config", "--set", "--now", "--repo", "--remote", "--host")

_ROW_RE = re.compile(r"^\| (`afk ([a-z-]+)(?:\\.|[^|\\])*?) \| ", re.M)


def _value(action):
    """What a row shows for an argument's value: its metavar, else its choices."""
    if action.metavar:
        return action.metavar
    return "\\|".join(action.choices) if action.choices else action.dest


def usage(name, parser):
    """One subcommand's signature as the table writes it: positionals, then each
    flag in the order it is defined, optional ones in brackets."""
    parts = [f"afk {name}"]
    for action in parser._actions:
        if not action.option_strings:
            parts.append(f"<{_value(action)}>")
            continue
        flag = action.option_strings[-1]
        if flag in SHARED:
            continue
        text = flag if action.nargs == 0 else f"{flag} <{_value(action)}>"
        parts.append(text if action.required else f"[{text}]")
    return " ".join(parts)


def render(text):
    """`text` (tools.md) with the first cell of every `afk <subcommand>` row
    replaced by that subcommand's signature. Idempotent; a row naming a subcommand
    that does not exist is an error."""
    subs = afk.build_parser().subcommands

    def cell(m):
        if m.group(2) not in subs:
            raise ValueError(f"tools.md has a row for `afk {m.group(2)}`, which is not a subcommand")
        return f"| `{usage(m.group(2), subs[m.group(2)])}` | "
    return _ROW_RE.sub(cell, text)


def main():
    with open(TOOLS_MD) as f:
        text = f.read()
    new = render(text)
    if "--check" in sys.argv[1:]:
        sys.exit(0 if new == text else "references/tools.md is stale — run scripts/gen_tools_doc.py")
    with open(TOOLS_MD, "w") as f:
        f.write(new)


if __name__ == "__main__":
    main()

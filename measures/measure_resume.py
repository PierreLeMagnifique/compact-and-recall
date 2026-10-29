"""How a session resumes after a compaction, measured on the SQLite index. Read-only.

What it counts, for every compact_summary passage inside a window
  It walks the next passages of the same session (kinds tool, human, agent; at most --max-calls tool calls)
  until the first "productive" tool call, and records:
    - calls      : tool calls examined before the first productive call,
    - reads      : read-only calls before it (Read, Grep, Glob, ToolSearch, or a shell command matching
                   --read-commands: cat, head, tail, sed -n, grep, rg, ls, dir, find, wc, type, Get-Content,
                   plus any memory tool),
    - memory     : calls to the memory tools (--memory-tools, default `search.py|episodes.py|entity.py`),
    - long       : full re-reads of a long document (--long-docs, default `README|HANDOVER|JOURNAL|CHANGELOG`):
                   a Read without "limit", or cat / Get-Content / type of such a file without a pipe,
    - minutes    : minutes between the compaction and the first productive call (ignored above 120 min).
  A human message ends the walk (the person took over). Only the command part of a tool passage is
  examined, not its result: the text is cut at the first result separator (--result-separator, a regex).

Windows
  --before START END --after START END   two windows to compare (ISO UTC timestamps), or
  --since START [--until END]            one window.
  --db overrides the index path (default cr_config.db_path()).

Output, per window
  number of compactions and agents; median, mean, max of read-only calls before the first productive call;
  median and mean minutes; share of resumes that used the memory tools; share that re-read a long document
  in full; the same per agent (n, median reads, memory %, long doc %).

What it never does
  Opens the index read-only (file:...?mode=ro) and never writes. Never prints the text of a passage.
"""
import argparse
import collections
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import cr_config  # noqa: E402

DEFAULT_MEMORY_TOOLS = r"search\.py|episodes\.py|entity\.py"
DEFAULT_LONG_DOCS = r"README|HANDOVER|JOURNAL|CHANGELOG"
DEFAULT_READ_COMMANDS = r"^\s*(cat|head|tail|sed -n|grep|rg|ls|dir|find|wc|type|Get-Content|tail -c)\b"
DEFAULT_RESULT_SEPARATOR = "\\n(?:->|=>|\u2192) "  # newline then an arrow: the result starts there
READ_TOOLS = ("Read", "Grep", "Glob", "ToolSearch")
SHELL_TOOLS = ("Bash", "PowerShell")


def dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def full_reread(tool, cmd, rx_long):
    """Only a read of the whole file counts: Read without limit, or cat / Get-Content / type without a pipe."""
    if tool == "Read":
        return '"limit"' not in cmd
    m = re.search(r"\b(cat|Get-Content|type)\s+([^|;&]*)(?:$|;|&&)", cmd, re.I)
    return bool(m and rx_long.search(m.group(2))) and not re.search(r"\bcat\s*>>?|<<", cmd)


def measure(cx, start, end, rx):
    out = []
    for sid, agent, ts in cx.execute("SELECT session, agent, ts FROM passages WHERE kind = 'compact_summary' "
                                     "AND ts >= ? AND ts < ? ORDER BY ts", (start, end)):
        rows = cx.execute("SELECT ts, kind, tool, text FROM passages WHERE session = ? AND ts > ? "
                          "AND kind IN ('tool', 'human', 'agent') ORDER BY ts LIMIT ?", (sid, ts, rx["max_calls"])).fetchall()
        n_read = n_mem = n_long = n_calls = 0
        productive_ts = None
        for t2, kind, tool, text in rows:
            if kind == "human":
                break
            if kind != "tool":
                continue
            n_calls += 1
            cmd = rx["sep"].split(text or "", 1)[0]
            is_mem = bool(rx["mem"].search(cmd))
            if is_mem:
                n_mem += 1
            if rx["long"].search(cmd) and full_reread(tool, cmd, rx["long"]):
                n_long += 1
            reading = tool in READ_TOOLS or (tool in SHELL_TOOLS and (is_mem or rx["read"].search(cmd)))
            if reading:
                n_read += 1
            else:
                productive_ts = t2
                break
        minutes = (dt(productive_ts) - dt(ts)).total_seconds() / 60 if productive_ts else None
        out.append(dict(agent=agent, calls=n_calls, reads=n_read, mem=n_mem, long=n_long, minutes=minutes))
    return out


def summarize(name, res):
    if not res:
        print(f"{name}: no compaction")
        return
    mins = [r["minutes"] for r in res if r["minutes"] is not None and r["minutes"] < 120]
    n = len(res)
    print(f"{name}: {n} compactions, {len({r['agent'] for r in res})} agents")
    print(f"  read-only calls before the first productive call: median {statistics.median(r['reads'] for r in res)}, "
          f"mean {statistics.mean(r['reads'] for r in res):.1f}, max {max(r['reads'] for r in res)}")
    if mins:
        print(f"  minutes before the first productive call: median {statistics.median(mins):.1f}, mean {statistics.mean(mins):.1f}")
    else:
        print("  minutes before the first productive call: n/a")
    with_mem = sum(1 for r in res if r["mem"])
    with_long = sum(1 for r in res if r["long"])
    print(f"  resumes that used the memory tools: {with_mem} / {n} ({100 * with_mem / n:.0f} %)")
    print(f"  resumes that re-read a long document in full: {with_long} / {n} ({100 * with_long / n:.0f} %)")
    per = collections.defaultdict(list)
    for r in res:
        per[r["agent"]].append(r)
    print("  per agent (n, median reads, memory %, long doc %):",
          {a: (len(v), statistics.median(x["reads"] for x in v), round(100 * sum(1 for x in v if x["mem"]) / len(v)),
               round(100 * sum(1 for x in v if x["long"]) / len(v)))
           for a, v in sorted(per.items(), key=lambda x: -len(x[1]))[:12]})


def add_window_args(ap):
    ap.add_argument("--before", nargs=2, metavar=("START", "END"), help="first window, ISO UTC")
    ap.add_argument("--after", nargs=2, metavar=("START", "END"), help="second window, ISO UTC")
    ap.add_argument("--since", help="single window start, ISO UTC")
    ap.add_argument("--until", default="9999", help="single window end, ISO UTC (default: no end)")
    ap.add_argument("--db", help="index path (default: cr_config.db_path())")


def windows_of(a, ap):
    wins = []
    if a.before:
        wins.append(("BEFORE " + " -> ".join(a.before), a.before[0], a.before[1]))
    if a.after:
        wins.append(("AFTER " + " -> ".join(a.after), a.after[0], a.after[1]))
    if a.since:
        wins.append((f"WINDOW {a.since} -> {a.until}", a.since, a.until))
    if not wins:
        ap.error("give --before START END and/or --after START END, or --since START [--until END]")
    return wins


def open_ro(db):
    db = db or cr_config.db_path()
    if not os.path.exists(db):
        sys.exit(f"no index at {db}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description="Resume behaviour after each compaction (read-only).")
    add_window_args(ap)
    ap.add_argument("--memory-tools", default=DEFAULT_MEMORY_TOOLS, help="regex (default: %(default)s)")
    ap.add_argument("--long-docs", default=DEFAULT_LONG_DOCS, help="regex (default: %(default)s)")
    ap.add_argument("--read-commands", default=DEFAULT_READ_COMMANDS, help="regex of read-only shell commands")
    ap.add_argument("--result-separator", default=DEFAULT_RESULT_SEPARATOR,
                    help="regex that separates a tool command from its result in a tool passage")
    ap.add_argument("--max-calls", type=int, default=40, help="passages examined after each compaction")
    a = ap.parse_args()
    rx = {"mem": re.compile(a.memory_tools, re.I), "long": re.compile(a.long_docs, re.I),
          "read": re.compile(a.read_commands, re.I), "sep": re.compile(a.result_separator), "max_calls": a.max_calls}
    cx = open_ro(a.db)
    for name, start, end in windows_of(a, ap):
        summarize(name, measure(cx, start, end, rx))


if __name__ == "__main__":
    main()

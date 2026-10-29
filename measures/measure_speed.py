"""Working speed and memory usage over a window, measured on the SQLite index. Read-only.

What it counts, per window
  1. median gap in seconds between two successive tool calls of the same session (the speed of one step);
     gaps above --max-gap seconds (default 300) are excluded as waiting time; also per agent (n, median),
  2. compactions in the window and the share of resumes that call a memory tool within the next
     --window-calls tool calls (default 15),
  3. memory tool calls over the whole window, per 1000 tool calls (--memory-tools regex, default
     `search.py|episodes.py|entity.py`),
  4. human messages per 1000 tool calls (a proxy for how often the person has to step in).

Windows
  --before START END --after START END   two windows to compare (ISO UTC timestamps), or
  --since START [--until END]            one window.
  --db overrides the index path (default cr_config.db_path()).

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


def dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def period(cx, name, start, end, rx_mem, max_gap, window_calls):
    rows = cx.execute("SELECT session, agent, ts FROM passages WHERE kind = 'tool' AND ts >= ? AND ts < ? "
                      "ORDER BY session, ts", (start, end)).fetchall()
    gaps = collections.defaultdict(list)
    prev = {}
    for sid, agent, ts in rows:
        if sid in prev:
            try:
                g = (dt(ts) - dt(prev[sid])).total_seconds()
            except ValueError:
                g = -1
            if 0 < g <= max_gap:
                gaps[agent].append(g)
        prev[sid] = ts
    all_gaps = [g for v in gaps.values() for g in v]
    print(f"\n== {name}: {len(rows)} tool calls, {len({r[0] for r in rows})} sessions")
    if all_gaps:
        print(f"  median gap between two tool calls: {statistics.median(all_gaps):.1f} s (mean {statistics.mean(all_gaps):.1f} s)")
        print("  per agent (n, median s):",
              {a: (len(v), round(statistics.median(v), 1)) for a, v in sorted(gaps.items(), key=lambda x: -len(x[1]))[:10]})
    else:
        print("  median gap between two tool calls: n/a")
    compactions = cx.execute("SELECT session, agent, ts FROM passages WHERE kind = 'compact_summary' AND ts >= ? AND ts < ?",
                             (start, end)).fetchall()
    with_mem = 0
    for sid, agent, ts in compactions:
        following = cx.execute("SELECT text FROM passages WHERE session = ? AND ts > ? AND kind = 'tool' ORDER BY ts LIMIT ?",
                               (sid, ts, window_calls)).fetchall()
        if any(rx_mem.search(t[0] or "") for t in following):
            with_mem += 1
    print(f"  compactions: {len(compactions)}; resumes that query the memory within the next {window_calls} calls: "
          f"{with_mem} ({100 * with_mem / max(1, len(compactions)):.0f} %)")
    n_mem = sum(1 for (t,) in cx.execute("SELECT text FROM passages WHERE kind = 'tool' AND ts >= ? AND ts < ?", (start, end))
                if rx_mem.search(t or ""))
    print(f"  memory tool calls: {n_mem}, that is {1000 * n_mem / max(1, len(rows)):.1f} per 1000 tool calls")
    n_h = cx.execute("SELECT count(*) FROM passages WHERE kind = 'human' AND ts >= ? AND ts < ?", (start, end)).fetchone()[0]
    print(f"  human messages: {n_h}, that is {1000 * n_h / max(1, len(rows)):.1f} per 1000 tool calls")


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description="Working speed and memory usage over a window (read-only).")
    ap.add_argument("--before", nargs=2, metavar=("START", "END"), help="first window, ISO UTC")
    ap.add_argument("--after", nargs=2, metavar=("START", "END"), help="second window, ISO UTC")
    ap.add_argument("--since", help="single window start, ISO UTC")
    ap.add_argument("--until", default="9999", help="single window end, ISO UTC (default: no end)")
    ap.add_argument("--db", help="index path (default: cr_config.db_path())")
    ap.add_argument("--memory-tools", default=DEFAULT_MEMORY_TOOLS, help="regex (default: %(default)s)")
    ap.add_argument("--max-gap", type=float, default=300, help="gaps above this many seconds are waiting time")
    ap.add_argument("--window-calls", type=int, default=15, help="tool calls examined after each compaction")
    a = ap.parse_args()
    wins = []
    if a.before:
        wins.append(("BEFORE " + " -> ".join(a.before), a.before[0], a.before[1]))
    if a.after:
        wins.append(("AFTER " + " -> ".join(a.after), a.after[0], a.after[1]))
    if a.since:
        wins.append((f"WINDOW {a.since} -> {a.until}", a.since, a.until))
    if not wins:
        ap.error("give --before START END and/or --after START END, or --since START [--until END]")
    db = a.db or cr_config.db_path()
    if not os.path.exists(db):
        sys.exit(f"no index at {db}")
    cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rx_mem = re.compile(a.memory_tools, re.I)
    for name, start, end in wins:
        period(cx, name, start, end, rx_mem, a.max_gap, a.window_calls)


if __name__ == "__main__":
    main()

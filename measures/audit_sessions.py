"""Per-session audit of Claude Code transcripts: cost proxy, context size, compactions, subagents,
memory tool usage, repeated commands and re-reads of long documents. Read-only.

How to call it
  python audit_sessions.py --since 2026-01-01T22:00:00Z [--until 2026-01-02T08:00:00Z]
  Transcripts come from cr_config.load()["transcripts_glob"]; only files modified after --since are opened,
  and each is read backwards so that a long transcript stops as soon as it leaves the window.

What it counts, per main session (subagent transcripts are attributed to their parent session)
  - API calls per model, first and last call time (local), total cost proxy and per-call average,
  - context size per call = input + cache write + cache read tokens (average and max),
  - cache written, cache read, output tokens,
  - subagent calls and their cost proxy, per model,
  - compactions (time and preTokens of each compact_boundary),
  - human messages (user turns that are plain text, not meta, not a compact summary),
  - recall hook attachments (hook attachments whose JSON contains --recall-marker),
  - memory tool calls (tool inputs matching --memory-tools), the 8 most used tools,
  - Read calls on long documents (file_path matching --long-docs),
  - cost proxy per hour of the day, and Bash/PowerShell commands repeated 4 times or more.

Cost proxy (in "input-token equivalents", one call = sum of weighted token counts)
  input x1, 5-minute cache write x1.25, 1-hour cache write x2, cache read x0.1, output x5.
  When the usage block has no cache_creation breakdown, cache_creation_input_tokens is counted as 5-minute writes.
  All weights can be overridden: --w-cache-write, --w-cache-write-1h, --w-cache-read, --w-output.
  These are relative weights for comparing sessions, not a price.

What it never does
  Never writes anything. Never prints the content of a message, only counts, tool names and the first
  110 characters of repeated shell commands.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import cr_config  # noqa: E402

DEFAULT_MEMORY_TOOLS = r"search\.py|episodes\.py|entity\.py|briefing\.py"
DEFAULT_LONG_DOCS = r"(README|HANDOVER|JOURNAL|CHANGELOG)"


def lines_from_end(path, block=8 << 20):
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        rest = b""
        while pos > 0:
            n = min(block, pos)
            pos -= n
            f.seek(pos)
            chunk = f.read(n) + rest
            parts = chunk.split(b"\n")
            rest = parts[0]
            for line in reversed(parts[1:]):
                yield line
        if rest:
            yield rest


def local(ts, fmt="%Y-%m-%d %H:%M"):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except (ValueError, AttributeError):
        return "?"


def session_of(path):
    parts = os.path.normpath(path).split(os.sep)
    if "subagents" in parts:
        i = parts.index("subagents")
        return parts[i - 1] if i > 0 else "?", True
    return os.path.splitext(os.path.basename(path))[0], False


def new_session():
    return {"models": collections.Counter(), "tools": collections.Counter(), "mem": collections.Counter(),
            "cmds": collections.Counter(), "docs": collections.Counter(), "compact": [], "ctx": [],
            "hours": collections.Counter(), "human": 0, "seen": set(), "sub_calls": 0, "sub_eq": 0.0,
            "sub_models": collections.Counter(), "first": None, "last": None, "recall": 0,
            "eq": 0.0, "cw": 0, "cr": 0, "out": 0}


def make_weight(w):
    def weight(u):
        cc = u.get("cache_creation") or {}
        c1h = cc.get("ephemeral_1h_input_tokens", 0)
        c5m = cc.get("ephemeral_5m_input_tokens", 0)
        if c1h + c5m == 0:
            c5m = u.get("cache_creation_input_tokens", 0)
        return (u.get("input_tokens", 0) + w["cache_write"] * c5m + w["cache_write_1h"] * c1h
                + w["cache_read"] * u.get("cache_read_input_tokens", 0) + w["output"] * u.get("output_tokens", 0))
    return weight


def audit(since, until, weights, rx_mem, rx_long, recall_marker):
    weight = make_weight(weights)
    sessions = collections.defaultdict(new_session)
    mtime_floor = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
    for path in glob.glob(cr_config.load()["transcripts_glob"], recursive=True):
        try:
            if os.path.getmtime(path) < mtime_floor:
                continue
        except OSError:
            continue
        sess, sub = session_of(path)
        s = sessions[sess]
        too_old = 0
        for line in lines_from_end(path):
            if not line.startswith(b"{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = d.get("timestamp", "")
            if ts and ts < since:
                too_old += 1
                if too_old > 200:
                    break
                continue
            if not ts or ts >= until:
                continue
            if d.get("type") == "system" and d.get("subtype") == "compact_boundary" and not sub:
                s["compact"].append((ts, (d.get("compactMetadata") or {}).get("preTokens")))
            if d.get("type") == "attachment" and not sub:
                a = d.get("attachment") or {}
                if str(a.get("type", "")).startswith("hook") and recall_marker in json.dumps(a, ensure_ascii=False)[:3000].lower():
                    s["recall"] += 1
            if d.get("type") == "user" and not sub and not d.get("isMeta") and not d.get("isCompactSummary"):
                c = (d.get("message") or {}).get("content")
                if isinstance(c, str) and not c.startswith("<"):
                    s["human"] += 1
            m = d.get("message") or {}
            if d.get("type") != "assistant":
                continue
            if not sub:
                for b in m.get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name", "?")
                    s["tools"][name] += 1
                    inp = b.get("input") or {}
                    txt = json.dumps(inp, ensure_ascii=False)
                    for hit in set(rx_mem.findall(txt)):
                        s["mem"][hit if isinstance(hit, str) else hit[0]] += 1
                    if name in ("Bash", "PowerShell"):
                        s["cmds"][(inp.get("command") or "")[:110]] += 1
                    if name == "Read":
                        mt = rx_long.search(inp.get("file_path", ""))
                        if mt:
                            s["docs"][mt.group(0)] += 1
            u = m.get("usage")
            if not u:
                continue
            mid = m.get("id") or d.get("uuid")
            if (path, mid) in s["seen"]:  # one API call is often written as several lines
                continue
            s["seen"].add((path, mid))
            eq = weight(u)
            model = m.get("model", "?")
            if sub:
                s["sub_calls"] += 1
                s["sub_eq"] += eq
                s["sub_models"][model] += 1
                continue
            s["models"][model] += 1
            s["ctx"].append(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                            + u.get("cache_read_input_tokens", 0))
            s["hours"][local(ts, "%H")] += eq
            s["eq"] += eq
            s["cw"] += u.get("cache_creation_input_tokens", 0)
            s["cr"] += u.get("cache_read_input_tokens", 0)
            s["out"] += u.get("output_tokens", 0)
            s["first"] = min(s["first"] or ts, ts)
            s["last"] = max(s["last"] or ts, ts)
    return sessions


def report(sessions):
    total = 0.0
    for sess, s in sorted(sessions.items(), key=lambda x: -(x[1]["eq"] + x[1]["sub_eq"])):
        n = len(s["ctx"])
        if not n and not s["sub_calls"]:
            continue
        eq = s["eq"]
        total += eq + s["sub_eq"]
        print(f"=== {sess[:8]}: {n} calls {dict(s['models'])}, from {local(s['first']) if s['first'] else '-'} "
              f"to {local(s['last']) if s['last'] else '-'}")
        if n:
            print(f"  cost proxy {eq / 1e6:.2f} M; per call {eq / n / 1e3:.1f} k; context mean {sum(s['ctx']) / n / 1e3:.0f} k, "
                  f"max {max(s['ctx']) / 1e3:.0f} k; cache written {s['cw'] / 1e6:.2f} M, read {s['cr'] / 1e6:.1f} M, "
                  f"output {s['out'] / 1e3:.0f} k")
        print(f"  subagents: {s['sub_calls']} calls, {s['sub_eq'] / 1e6:.2f} M proxy {dict(s['sub_models'])}")
        print(f"  compactions: {len(s['compact'])} "
              + ", ".join(f"{local(t, '%H:%M')}({(p or 0) // 1000}k)" for t, p in sorted(s["compact"])))
        print(f"  human messages: {s['human']}; recall hook hits: {s['recall']}; memory tools: {dict(s['mem'])}")
        print(f"  tools: {dict(s['tools'].most_common(8))}")
        print(f"  long documents read: {dict(s['docs'])}")
        print("  per hour (k proxy): " + " ".join(f"{h}h={v / 1e3:.0f}" for h, v in sorted(s["hours"].items())))
        for cmd, c in s["cmds"].most_common(3):
            if c >= 4:
                print(f"  command repeated x{c}: {cmd}")
    print(f"TOTAL cost proxy: {total / 1e6:.1f} M")


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description="Per-session audit of Claude Code transcripts (read-only).")
    ap.add_argument("--since", required=True, help="ISO UTC start, for example 2026-01-01T22:00:00Z")
    ap.add_argument("--until", default="9999", help="ISO UTC end (default: no end)")
    ap.add_argument("--memory-tools", default=DEFAULT_MEMORY_TOOLS, help="regex of memory tool calls (default: %(default)s)")
    ap.add_argument("--long-docs", default=DEFAULT_LONG_DOCS, help="regex of long documents (default: %(default)s)")
    ap.add_argument("--recall-marker", default="recall", help="substring that identifies the recall hook attachment")
    ap.add_argument("--w-cache-write", type=float, default=1.25)
    ap.add_argument("--w-cache-write-1h", type=float, default=2.0)
    ap.add_argument("--w-cache-read", type=float, default=0.1)
    ap.add_argument("--w-output", type=float, default=5.0)
    a = ap.parse_args()
    weights = {"cache_write": a.w_cache_write, "cache_write_1h": a.w_cache_write_1h,
               "cache_read": a.w_cache_read, "output": a.w_output}
    report(audit(a.since, a.until, weights, re.compile(a.memory_tools, re.I), re.compile(a.long_docs, re.I),
                 a.recall_marker.lower()))


if __name__ == "__main__":
    main()

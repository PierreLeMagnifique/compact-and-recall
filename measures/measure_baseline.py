"""Baseline of a session after each compaction: what the first API call carries, component by component.
Read-only.

How it works
  After a compact_boundary, Claude Code writes into the transcript the shape of the next request
  (system prompt, tool definitions), the attachments injected again (instruction files, hooks, files re-read,
  compact summary) and the usage of the first call. This tool splits the characters sent between components,
  then converts them to tokens at the real rate of that call (context tokens / characters sent).
  The split is approximate, the total is exact.

How to call it
  python measure_baseline.py <session-id> [<session-id>...] [--since 2026-01-01T22:00] [--detail]
  The transcript is looked up in cr_config.load()["transcripts_glob"] by file name <session-id>.jsonl
  (subagent files excluded; the largest match wins). --since skips everything before that UTC timestamp
  (the file is scanned by blocks from its end). --detail prints the split for every compaction, not only the last.

What it prints
  One table per session: boundary time (local), tokens before and after, summary duration, total stop time
  (last call before the boundary -> first call after), summary size in characters, first call context and
  the part re-written into the cache. Then the component split: system prompt, the 4 largest tool definitions,
  the other tool definitions, each instruction file, each hook, files re-read after compaction, the compact
  summary, the next message (human, relay or tool result).

What it never does
  Never writes anything. Never prints the content of a component, only its size.
"""
import argparse
import glob
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import cr_config  # noqa: E402


def transcript_of(session):
    """Largest transcript named <session>.jsonl under transcripts_glob, excluding subagent files."""
    hits = [p for p in glob.glob(cr_config.load()["transcripts_glob"], recursive=True)
            if os.path.splitext(os.path.basename(p))[0] == session and "subagents" not in os.path.normpath(p).split(os.sep)]
    return max(hits, key=os.path.getsize) if hits else None


def local(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return ts or "?"


def lines_since(path, since):
    """JSON lines from the first timestamp >= since (block search from the end of the file)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = size
        while pos > 0 and since:
            pos = max(0, pos - (64 << 20))
            f.seek(pos)
            f.readline()
            line = f.readline()
            try:
                ts = json.loads(line).get("timestamp") or ""
            except ValueError:
                ts = ""
            if ts and ts < since:
                break
        if not since:
            pos = 0
        f.seek(pos)
        if pos:
            f.readline()
        for raw in f:
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def instruction_name(path):
    return "instruction file " + os.path.basename(path or "?")


def split_components(events):
    """events = lines from the boundary up to and including the first API call."""
    parts = OrderedDict()

    def add(key, n):
        parts[key] = parts.get(key, 0) + n

    usage = None
    for o in events:
        t = o.get("type")
        if t == "api-request-shape":
            shape = o.get("shape") or {}
            for b in shape.get("system") or []:
                add("system prompt", len(b.get("text", "")) if isinstance(b, dict) else len(str(b)))
            tools = shape.get("tools") or []
            big = sorted(tools, key=lambda x: -len(json.dumps(x, ensure_ascii=False)))
            for x in big[:4]:
                add(f"tool {x.get('name', '?')} (definition)", len(json.dumps(x, ensure_ascii=False)))
            if len(big) > 4:
                add(f"{len(tools) - 4} other tools (definitions)", sum(len(json.dumps(x, ensure_ascii=False)) for x in big[4:]))
        elif t == "attachment":
            a = o.get("attachment") or {}
            at = a.get("type")
            if at == "instructions":
                for fi in a.get("files") or []:
                    add(instruction_name(fi.get("path") or fi.get("filePath")), len(json.dumps(fi, ensure_ascii=False)))
            elif at == "hook_success":
                add("hook " + str(a.get("hookName") or a.get("hookEvent")), len(json.dumps(a, ensure_ascii=False)))
            elif at == "file":
                add("files re-read after compaction", len(json.dumps(a, ensure_ascii=False)))
            elif at == "prompt_snapshot":
                continue  # local snapshot, not sent
            else:
                add(f"attachment {at}", len(json.dumps(a, ensure_ascii=False)))
        elif t == "user" and o.get("isCompactSummary"):
            add("compact summary", len(json.dumps((o.get("message") or {}).get("content"), ensure_ascii=False)))
        elif t == "user":
            add("next message (human, relay, tool result)",
                len(json.dumps((o.get("message") or {}).get("content"), ensure_ascii=False)))
        elif t == "assistant" and (o.get("message") or {}).get("usage"):
            usage = o["message"]["usage"]
            break
    return parts, usage


def print_split(ts, parts, ctx):
    total = sum(parts.values())
    rate = ctx / total if total else 0
    chars_per_token = f"{1 / rate:.2f}" if rate else "?"
    print(f"\nBaseline after the boundary of {local(ts)}: {ctx} tokens for {total} characters sent "
          f"(1 token ~ {chars_per_token} characters; split in proportion to characters)\n")
    print("| component | characters | tokens (~) | share |")
    print("|---|---|---|---|")
    for k, n in sorted(parts.items(), key=lambda kv: -kv[1]):
        print(f"| {k} | {n} | {n * rate:.0f} | {100 * n / total:.0f} % |")


def measure(session, since, detail):
    path = transcript_of(session)
    if not path:
        print(f"{session}: transcript not found under {cr_config.load()['transcripts_glob']}")
        return
    evs = list(lines_since(path, since))
    boundaries = [i for i, o in enumerate(evs) if o.get("type") == "system" and o.get("subtype") == "compact_boundary"
                  and (o.get("timestamp") or "") >= since]
    print(f"\n## {session[:8]} ({os.path.getsize(path) >> 20} MB): {len(boundaries)} compaction(s)"
          + (f" since {local(since)}" if since else "") + "\n")
    print("| boundary | before (tokens) | after (tokens) | summary duration | total stop | summary (chars) "
          "| 1st call: context | of which written to cache |")
    print("|---|---|---|---|---|---|---|---|")
    last_split = None
    for i in boundaries:
        o = evs[i]
        md = o.get("compactMetadata") or {}
        before = next((evs[j] for j in range(i - 1, -1, -1) if evs[j].get("type") == "assistant"), None)
        end = next((j for j in range(i + 1, len(evs)) if evs[j].get("type") == "assistant"
                    and (evs[j].get("message") or {}).get("usage")), None)
        parts, usage = split_components(evs[i:end + 1] if end is not None else evs[i:])
        summary = parts.get("compact summary", 0)
        if usage:
            ctx = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            written = usage.get("cache_creation_input_tokens", 0)
        else:
            ctx = written = 0
        stop = "?"
        if before and end is not None and before.get("timestamp") and evs[end].get("timestamp"):
            d = (datetime.fromisoformat(evs[end]["timestamp"].replace("Z", "+00:00"))
                 - datetime.fromisoformat(before["timestamp"].replace("Z", "+00:00"))).total_seconds()
            stop = f"{d:.0f} s"
        print(f"| {local(o.get('timestamp', ''))} | {md.get('preTokens', '?')} | {md.get('postTokens', '?')} | "
              f"{(md.get('durationMs') or 0) / 1000:.0f} s | {stop} | {summary} | {ctx} | {written} |")
        if usage and parts:
            last_split = (o.get("timestamp", ""), parts, ctx)
            if detail:
                print_split(*last_split)
    if last_split and not detail:
        print_split(*last_split)


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description="What the first call after each compaction carries (read-only).")
    ap.add_argument("sessions", nargs="+", help="session ids (transcript file names without .jsonl)")
    ap.add_argument("--since", default="", help="ISO UTC timestamp; ignore everything before it")
    ap.add_argument("--detail", action="store_true", help="component split for every compaction, not only the last")
    a = ap.parse_args()
    for session in a.sessions:
        measure(session, a.since, a.detail)


if __name__ == "__main__":
    main()

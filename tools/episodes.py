"""Frozen episodes: one dated Markdown file per compaction or end of task, never rewritten.

An episode is built from the RAW transcript lines written since the previous compaction, never
from a summary and without any model: the person's messages verbatim, the agent's last reports,
the files written or modified, the identifiers cited, the machine time. It is stored under
<memory_root>/<episodes.dir>/<agent>/EPISODE_<YYYY-MM-DD>_<HHhMM>_<source>.md and later picked up
by the indexer (kind "episode").

  python episodes.py hook                                   PreCompact hook: reads {session_id, transcript_path,
                                                            trigger, cwd} as JSON on stdin
  python episodes.py end --agent X [--transcript f.jsonl]   end of a task: freeze an episode of X's current session
  python episodes.py list [--agent X] [--limit N]           list the episode files, newest first
  python episodes.py import                                 compaction summaries already in the index become
                                                            "native_summary" episodes (summary of a summary: flagged)

What it never does: rewrite or delete an episode (a second episode in the same minute gets a numeric
suffix), write outside memory_root, write to the SQLite index (opened read-only here; the indexer is
the only writer), print a secret (the secrets filter runs on everything written, when available),
fail the session (the hook swallows its own errors and exits 0).
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cr_config  # noqa: E402

try:
    import secrets_filter as _sf

    def clean(text):
        """Secrets filter from tools/secrets_filter.py; identity if the module has no known entry point."""
        for name in ("clean", "scrub", "sanitize", "redact", "filter_text"):
            fn = getattr(_sf, name, None)
            if callable(fn):
                return fn(text)
        return text
except ImportError:
    def clean(text):
        return text

READ_MAX = 300 * 1024 * 1024    # never read further back than this in one transcript
HUMAN_CHARS_MAX = 60000         # characters kept for the person's messages (most recent first)
REPORTS = 8                     # last reports of the agent
REPORT_MAX = 2500
DEBOUNCE_SECONDS = 180          # a real compaction of the main session never comes back faster than this

RX_REMINDER = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")
RX_NATIVE_SUMMARY = re.compile(rb'"isCompactSummary":\s*true|"subtype":\s*"compact_boundary"')
# Files written by a shell command (not by Write/Edit): >, >>, tee, open(..., "w"|"a"),
# Set-Content / Add-Content / Out-File, cp or mv towards an absolute path.
_PATH = r'("?(?:[A-Za-z]:[\\/]|/[a-z]/|/|~/)[^"\s;|&<>()]+\.(?:md|tsv|csv|py|sh|ps1|json|txt|html|js|log)"?)'
RX_SHELL_WRITE = re.compile(
    r"(?:>>?|\btee\s+(?:-a\s+)?)\s*" + _PATH +
    r"|open\(\s*r?[\"']((?:[A-Za-z]:[\\/]|/)[^\"']+)[\"']\s*,\s*[\"'][wax]" +
    r"|(?:Set-Content|Add-Content|Out-File)\s+(?:-(?:Path|FilePath|LiteralPath)\s+)?" + _PATH +
    r"|\b(?:cp|mv|copy|move|Copy-Item|Move-Item)\s+(?:-\w+\s+)*\S+\s+" + _PATH)


def machine_time():
    return datetime.now().astimezone()


def local(ts):
    """UTC ISO timestamp from a transcript -> local '%Y-%m-%d %H:%M'."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return "?"


def safe_name(agent):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", agent or "unknown") or "unknown"


def search_command(agent):
    search = os.path.join(HERE, "search.py").replace("\\", "/")
    return f'python -X utf8 "{search}" <words> --agent {agent}'


def agent_of(session, cwd=None, transcript_path=None):
    """Registry (SessionStart hook) first, then the cwd rule, then the project folder of the transcript."""
    info = cr_config.session_info(session)
    if info.get("agent"):
        return info["agent"]
    if cwd:
        return cr_config.current_agent(cwd)
    return cr_config.agent_of(session, transcript_path)


def find_transcript(session):
    """Main transcript of a session (not a subagent file), newest if several match."""
    if not session:
        return None
    found = [p for p in glob.glob(cr_config.load()["transcripts_glob"], recursive=True)
             if os.path.basename(p) == session + ".jsonl" and "subagents" not in p.replace("\\", "/").split("/")]
    return max(found, key=os.path.getmtime) if found else None


def session_of_agent(agent):
    """Most recently started session registered for this agent, or None."""
    try:
        with open(cr_config.sessions_path(), encoding="utf-8") as f:
            sessions = json.load(f)
    except (OSError, ValueError):
        return None
    best = None
    for sid, info in sessions.items():
        if info.get("agent") == agent and (best is None or (info.get("last_start") or "") >= (best[1].get("last_start") or "")):
            best = (sid, info)
    return best[0] if best else None


def lines_since_last_compaction(path):
    """Raw JSON lines after the last compaction marker (excluded), read backwards, returned in order."""
    size = os.path.getsize(path)
    pos, rest, lines = size, b"", []
    with open(path, "rb") as f:
        while pos > 0 and size - pos < READ_MAX:
            n = min(8 << 20, pos)
            pos -= n
            f.seek(pos)
            chunks = (f.read(n) + rest).split(b"\n")
            rest = chunks[0]
            for raw in reversed(chunks[1:]):
                if not raw.strip():
                    continue
                if RX_NATIVE_SUMMARY.search(raw):
                    return list(reversed(lines))
                lines.append(raw)
        if rest.strip() and pos == 0:
            if RX_NATIVE_SUMMARY.search(rest):
                return list(reversed(lines))
            lines.append(rest)
    return list(reversed(lines))


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _exists(p):
    real = p[:-len(" (shell)")] if p.endswith(" (shell)") else p
    if sys.platform == "win32":
        real = re.sub(r"^/([a-z])/", lambda m: m.group(1).upper() + ":/", real)  # Git Bash form -> drive form
    return os.path.exists(os.path.expanduser(real))


def build(path, agent, session, source, trigger=None):
    """Return (markdown text, machine time) for the raw lines since the last compaction of `path`."""
    cfg = cr_config.load()
    human = cfg.get("human_label") or "human"
    relay_markers = tuple(cfg.get("relay_markers") or ())
    human_msgs, reports, written, first, last, corpus = [], [], {}, None, None, []
    for raw in lines_since_last_compaction(path):
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if o.get("type") not in ("user", "assistant"):
            continue
        ts = o.get("timestamp")
        first = first or ts
        last = ts or last
        content = (o.get("message") or {}).get("content")
        if o["type"] == "user":
            if o.get("isMeta") or o.get("isCompactSummary"):
                continue
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            t = RX_REMINDER.sub("", text_of(content)).strip()
            if not t or t.startswith(("<task-notification", "<command-", "<local-command")):
                continue
            if relay_markers and t.startswith(relay_markers):
                continue
            human_msgs.append((ts, t))
            corpus.append(t)
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                reports.append((ts, b["text"]))
                corpus.append(b["text"])
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b.get("name") in ("Write", "Edit", "NotebookEdit") and isinstance(inp.get("file_path"), str):
                    written[inp["file_path"]] = ts
                cmd = inp.get("command") if isinstance(inp.get("command"), str) else ""
                corpus.append(cmd)
                for m in RX_SHELL_WRITE.finditer(cmd):
                    target = next(g for g in m.groups() if g)
                    written.setdefault(target.strip("\"'") + " (shell)", ts)
    now = machine_time()
    whole = "\n".join(corpus)
    lines = [
        f"# Episode {agent} - {now.strftime('%Y-%m-%d %H:%M')} (machine time)",
        "",
        f"- **Source**: {source}{' (' + trigger + ')' if trigger else ''}, built from the raw messages since the "
        "previous compaction, without any model. Never rewritten.",
        f"- **Period covered**: {local(first)} -> {local(last)}",
        f"- **Session**: `{session}`; transcript `{path}`",
        "",
        f"## Messages from {human}, verbatim (oldest first)",
    ]
    kept, total = [], 0
    for ts, t in reversed(human_msgs):
        if total + len(t) > HUMAN_CHARS_MAX:
            kept.append((ts, t[:HUMAN_CHARS_MAX - total] + " [...]"))
            break
        kept.append((ts, t))
        total += len(t)
    if len(kept) < len(human_msgs):
        lines.append(f"_{len(human_msgs) - len(kept)} older message(s) not repeated here: they stay in the index._")
    for ts, t in reversed(kept):
        lines += ["", f"**{local(ts)}**", "> " + t.replace("\n", "\n> ")]
    if not human_msgs:
        lines.append(f"_No message from {human} in this period._")
    lines += ["", f"## Last reports of the agent ({min(REPORTS, len(reports))} of {len(reports)})"]
    for ts, t in reports[-REPORTS:]:
        lines += ["", f"**{local(ts)}**", t if len(t) <= REPORT_MAX else t[:REPORT_MAX] + " [...]"]
    lines += ["", f"## Files written or modified ({len(written)})"]
    lines += [f"- `{p}` ({local(ts)})" + ("" if _exists(p) else " - absent from disk when the episode was written (renamed or moved?)")
              for p, ts in sorted(written.items(), key=lambda kv: kv[1] or "")] or ["_none_"]
    lines += ["", "## Identifiers cited (mention counts)"]
    for name, pattern in (cfg.get("entity_patterns") or {}).items():
        seen = {}
        try:
            for m in re.findall(pattern, whole):
                seen[m] = seen.get(m, 0) + 1
        except re.error:
            continue
        if seen:
            top = sorted(seen.items(), key=lambda kv: -kv[1])[:40]
            lines.append(f"- **{name}**: " + ", ".join(f"{k} ({n})" if n > 1 else k for k, n in top))
    lines += ["", "## To find the details", f"`{search_command(agent)}` (every raw message is in the index)."]
    return clean("\n".join(lines)), now


def deposit(agent, text, when, source):
    """Write a new episode file; never overwrites (suffix _2, _3... within the same minute)."""
    folder = cr_config.episodes_dir(safe_name(agent))
    os.makedirs(folder, exist_ok=True)
    base = f"EPISODE_{when.strftime('%Y-%m-%d_%Hh%M')}_{source}"
    path = os.path.join(folder, base + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}_{n}.md")
        n += 1
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)
    return path


def hook():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    session = data.get("session_id") or ""
    path = data.get("transcript_path") or find_transcript(session)
    if not path or not os.path.exists(path):
        return
    # The compaction of a subagent fires this hook too: only the main session freezes an episode.
    if data.get("agent_id") or data.get("agent_type") or "subagents" in path.replace("\\", "/").split("/"):
        return
    agent = agent_of(session, data.get("cwd"), path)
    folder = cr_config.episodes_dir(safe_name(agent))
    recent = [p for p in glob.glob(os.path.join(folder, "EPISODE_*_precompact*.md"))
              if time.time() - os.path.getmtime(p) < DEBOUNCE_SECONDS]
    if recent:
        return
    text, when = build(path, agent, session, "precompact", data.get("trigger"))
    p = deposit(agent, text, when, "precompact")
    print(f"[memory] episode frozen before compaction: {p}")


def end(agent, transcript=None):
    session = None
    if transcript is None:
        session = session_of_agent(agent)
        registered = cr_config.session_info(session).get("transcript_path") if session else None
        transcript = registered if registered and os.path.exists(registered) else (find_transcript(session) if session else None)
    if not transcript or not os.path.exists(transcript):
        sys.exit(f"no transcript known for {agent}: give --transcript")
    session = session or os.path.splitext(os.path.basename(transcript))[0]
    text, when = build(transcript, agent, session, "end_of_task")
    print(deposit(agent, text, when, "end_of_task"))


def list_episodes(agent=None, limit=20):
    root = cr_config.episodes_dir(safe_name(agent)) if agent else cr_config.episodes_dir()
    files = sorted(glob.glob(os.path.join(root, "**", "EPISODE_*.md"), recursive=True), reverse=True)
    for p in files[:limit]:
        print(p)
    if not files:
        print("(no episode yet)")


def import_native():
    db = cr_config.db_path()
    if not os.path.exists(db):
        print("no index yet: nothing to import")
        return
    cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    done = 0
    for agent, session, ts, text in cx.execute(
            "SELECT coalesce(nullif(agent, '?'), 'session_' || substr(session, 1, 8)), session, ts, text "
            "FROM passages WHERE kind = 'compact_summary' AND subagent IS NULL ORDER BY ts"):
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        folder = cr_config.episodes_dir(safe_name(agent))
        if glob.glob(os.path.join(folder, f"EPISODE_{when.strftime('%Y-%m-%d_%Hh%M')}_native_summary*.md")):
            continue  # already imported
        head = (f"# Episode {agent} - {when.strftime('%Y-%m-%d %H:%M')} (native compaction summary)\n\n"
                f"- **Source**: compaction summary written by the assistant, imported as is on "
                f"{machine_time().strftime('%Y-%m-%d %H:%M')}. Caution: a summary of a summary, the person's words "
                f"are rephrased; the raw messages are in the index.\n- **Session**: `{session}`\n\n")
        deposit(agent, clean(head + text), when, "native_summary")
        done += 1
    cx.close()
    print(f"{done} native summary(ies) imported into {cr_config.episodes_dir()}")


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="PreCompact hook (JSON on stdin)")
    e = sub.add_parser("end", help="freeze an episode at the end of a task")
    e.add_argument("--agent", required=True)
    e.add_argument("--transcript", help="transcript file (default: the agent's last registered session)")
    ls = sub.add_parser("list", help="list episode files")
    ls.add_argument("--agent")
    ls.add_argument("--limit", type=int, default=20)
    sub.add_parser("import", help="turn indexed compaction summaries into episodes")
    a = ap.parse_args()
    if a.cmd == "hook":
        try:
            hook()
        except Exception as exc:  # a hook must never block the compaction
            print(f"[memory] episode not written: {type(exc).__name__} {exc}", file=sys.stderr)
    elif a.cmd == "end":
        end(a.agent, a.transcript)
    elif a.cmd == "list":
        list_episodes(a.agent, a.limit)
    else:
        import_native()


if __name__ == "__main__":
    main()

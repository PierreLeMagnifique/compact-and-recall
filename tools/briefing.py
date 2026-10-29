"""SessionStart hook: register the session, then print a short briefing of what a compaction loses.

Printed (at most budgets.briefing_chars characters, about 2000 tokens): the last messages of the
person VERBATIM (a summary rephrases them; these are authoritative), the standing decisions of the
project (last lines of the decisions file), the latest frozen episodes, the machine time, and the
command that searches everything else. Read-only on the index.

  python briefing.py --hook                   SessionStart hook: reads {session_id, cwd, transcript_path, source}
                                              as JSON on stdin; registers the session first (sessions.json)
  python briefing.py --agent X [--cwd DIR]    manual briefing for an agent (no registration)

What it never does: write anything except the session registry under memory_root, open the index
in write mode, print a secret (the secrets filter runs on the output, when available), fail the
session (errors go to stderr, exit code 0), print more than the budget.
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cr_config  # noqa: E402
from episodes import clean, local, safe_name, search_command  # noqa: E402

HUMAN_MESSAGES = 5
MESSAGE_MAX = 500
DECISIONS = 8
EPISODES_RECENT = 3

# A decision line: a heading or a bullet or a table row that names D<n>; fallback: level 2-3 headings.
RX_DECISION = re.compile(r"^(?:#{1,4} .*\bD\d+\b.*|- \*\*D\d+.*|\*\*D\d+.*|\| *D\d+ *\|.*|(?:- )?D\d+\b.*)$")
RX_TITLE = re.compile(r"^#{2,3} .+$")


def decisions(project_dir):
    """(path, last DECISIONS decision lines) from <project_dir>/<decisions_file>, or (None, [])."""
    if not project_dir:
        return None, []
    path = os.path.normpath(os.path.join(project_dir, cr_config.load()["decisions_file"]))
    if not os.path.exists(path):
        return None, []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip() for l in f]
    found = [l for l in lines if RX_DECISION.match(l)] or [l for l in lines if RX_TITLE.match(l)]
    return path, [l.lstrip("#-* |").replace("**", "").strip(" |")[:160] for l in found[-DECISIONS:]]


def latest_episodes(agent):
    out = []
    pattern = os.path.join(cr_config.episodes_dir(safe_name(agent)), "EPISODE_*.md")
    for path in sorted(glob.glob(pattern), reverse=True)[:EPISODES_RECENT]:
        m = re.search(r"EPISODE_(\d{4}-\d{2}-\d{2})_(\d{2})h(\d{2})_([a-z_]+?)(?:_\d+)?\.md$", path)
        if m:
            out.append((f"{m.group(1)} {m.group(2)}:{m.group(3)}", m.group(4), path))
    return out


def briefing(agent, project_dir=None):
    cfg = cr_config.load()
    human = cfg.get("human_label") or "human"
    budget = int(cfg["budgets"]["briefing_chars"])
    out = [f"## Memory: {agent}, {datetime.now().strftime('%Y-%m-%d %H:%M')} (machine time)"]
    messages = []
    db = cr_config.db_path()
    if os.path.exists(db):
        try:
            cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            messages = cx.execute(
                "SELECT ts, text FROM passages WHERE agent = ? AND kind = 'human' AND subagent IS NULL "
                "ORDER BY ts DESC LIMIT ?", (agent, HUMAN_MESSAGES)).fetchall()
            cx.close()
        except sqlite3.Error as exc:
            out.append(f"(index unavailable: {exc})")
    else:
        out.append("(no index yet: run tools/indexer.py once, or wait for the scheduled task)")
    if messages:
        out.append(f"\n**Last {len(messages)} messages from {human}, verbatim** (a summary rephrases them; these are authoritative):")
        for ts, t in reversed(messages):
            t = " ".join(t.split())
            out.append(f"- {local(ts)}: \"{t[:MESSAGE_MAX]}{' [...]' if len(t) > MESSAGE_MAX else ''}\"")
    path, decs = decisions(project_dir)
    if decs:
        out.append(f"\n**Recent decisions of the project** (`{path}`, reread before asking {human} anything):")
        out += [f"- {d}" for d in decs]
    # Episodes are read from disk, not from the index: the one frozen at the previous compaction is not indexed yet.
    episodes = latest_episodes(agent)
    if episodes:
        out.append("\n**Latest frozen episodes** (read the most recent one when unsure about the state of the work):")
        out += [f"- {when} {source}: `{p}`" for when, source, p in episodes]
    out.append(
        f"\n**Search the whole past** (raw messages of every session, secrets removed): `{search_command(agent)}` "
        "(options: `--kind human`, `--since 2026-01-31`, `--turn <session> <n>`). "
        f"Before asking {human} a question again, search whether it was already settled.")
    return clean("\n".join(out))[:budget]


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hook", action="store_true", help="SessionStart hook mode (JSON on stdin)")
    ap.add_argument("--agent", help="manual mode: agent name")
    ap.add_argument("--cwd", help="manual mode: project directory holding the decisions file")
    a = ap.parse_args()
    try:
        if a.hook:
            try:
                data = json.loads(sys.stdin.read() or "{}")
            except ValueError:
                data = {}
            session = data.get("session_id") or ""
            cwd = data.get("cwd") or os.getcwd()
            agent = cr_config.current_agent(cwd)
            try:
                extra = {"transcript_path": data.get("transcript_path"), "source": data.get("source"),
                         "last_start": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                cr_config.register_session(session, agent, cwd, extra={k: v for k, v in extra.items() if v})
            except Exception as exc:  # the registry must never block the briefing
                print(f"[memory] session not registered: {type(exc).__name__} {exc}", file=sys.stderr)
            project_dir = cwd
        else:
            if not a.agent:
                ap.error("--hook or --agent X is required")
            agent, project_dir = a.agent, a.cwd
        print(briefing(agent, project_dir))
    except Exception as exc:  # a start-up hook must never block the session
        print(f"[memory] briefing unavailable: {type(exc).__name__} {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()

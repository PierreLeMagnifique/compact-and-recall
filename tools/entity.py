"""Everything the memory knows about one entity of the graph. Read-only.

How to call it
  python entity.py D12 [--agent NAME]   a decision: its text, what it replaces, who talks about it
  python entity.py v1172                a version: first and last mentions, agents
  python entity.py deploy.py            a script or a file: who wrote it, when, where it is cited
  python entity.py src/tools            a folder: the files it contains
  --agent restricts scoped entities (decisions, questions) to one agent.

What it prints
  The entity header (mentions count, first and last date, local time), its stored text, its links
  (both directions, except `contains` which is summarised for folders), the agents that mention it and
  the last mentions with a short excerpt (human messages, agent replies and episodes first).

What it never does
  Opens the index in read-only mode (file:...?mode=ro) and never writes anything.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cr_config  # noqa: E402

LAST_MENTIONS = 6
MAX_SHOWN = 4


def local(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return ts or "?"


def excerpt(text, name, width=160):
    i = text.find(name)
    if i < 0:
        i = text.lower().find(name.lower())
    start = max(0, i - width // 2) if i >= 0 else 0
    return " ".join(text[start:start + width + len(name)].split())


def find(cx, name, agent):
    name_n = name.replace("\\", "/").rstrip("/")
    rows = cx.execute("SELECT id, type, name, scope, text FROM entities WHERE name = ? COLLATE NOCASE", (name_n,)).fetchall()
    if not rows:  # a file name or the end of a path
        rows = cx.execute("SELECT id, type, name, scope, text FROM entities WHERE type IN ('file', 'folder') "
                          "AND name LIKE ? ORDER BY length(name) LIMIT 8", ("%" + name_n,)).fetchall()
    if agent:
        matching = [r for r in rows if r[3] in ("", agent)]
        rows = matching or rows
    return rows


def show(cx, eid, type_, name, scope, text):
    n, first, last = cx.execute(
        "SELECT count(*), min(p.ts), max(p.ts) FROM mentions m JOIN passages p ON p.id = m.passage WHERE m.entity = ?",
        (eid,)).fetchone()
    print(f"## {type_} {name}{' (' + scope + ')' if scope else ''}: {n} mention(s), from {local(first)} to {local(last)}")
    if text:
        print("Text: " + " ".join(text.split())[:900])
    # sorted by relation name without its trailing digits (step_2 before step_10), then by length
    for relation, other, direction in cx.execute(
            "SELECT relation, other, direction FROM ("
            "SELECT l.relation, e.type || ' ' || e.name || CASE WHEN e.scope <> '' THEN ' (' || e.scope || ')' ELSE '' END "
            "AS other, 'to' AS direction FROM links l JOIN entities e ON e.id = l.target "
            "WHERE l.source = ? AND l.relation <> 'contains' "
            "UNION SELECT l.relation, e.type || ' ' || e.name || CASE WHEN e.scope <> '' THEN ' (' || e.scope || ')' ELSE '' END, "
            "'from' FROM links l JOIN entities e ON e.id = l.source WHERE l.target = ? AND l.relation <> 'contains') "
            "ORDER BY rtrim(relation, '0123456789'), length(relation), relation, other LIMIT 60", (eid, eid)):
        print(f"- {relation} {'->' if direction == 'to' else '<-'} {other}")
    if type_ == "folder":
        children = sorted({f[0].rsplit("/", 1)[-1].strip("\"'`,;)") for f in cx.execute(
            "SELECT e.name FROM links l JOIN entities e ON e.id = l.target WHERE l.source = ? AND l.relation = 'contains'",
            (eid,))})
        print(f"Contains {len(children)} name(s): " + ", ".join(children[:60]) + (" ..." if len(children) > 60 else ""))
    if type_ == "file":
        for agent, ts in cx.execute("SELECT e.name, max(l.since) FROM links l JOIN entities e ON e.id = l.source "
                                    "WHERE l.target = ? AND l.relation = 'wrote' GROUP BY 1", (eid,)):
            print(f"- written by {agent}, last time on {local(ts)}")
    agents = cx.execute("SELECT p.agent, count(*) FROM mentions m JOIN passages p ON p.id = m.passage WHERE m.entity = ? "
                        "GROUP BY 1 ORDER BY 2 DESC LIMIT 6", (eid,)).fetchall()
    if agents:
        print("Agents: " + ", ".join(f"{a} ({c})" for a, c in agents))
    print("Last mentions (human messages, agent replies and episodes first):")
    for pid, ts, agent, kind, t in cx.execute(
            "SELECT p.id, p.ts, p.agent, p.kind, p.text FROM mentions m JOIN passages p ON p.id = m.passage "
            "WHERE m.entity = ? ORDER BY p.kind IN ('human', 'agent', 'episode') DESC, p.ts DESC LIMIT ?",
            (eid, LAST_MENTIONS)):
        print(f"- [{pid}] {local(ts)} {agent} {kind}: {excerpt(t or '', name.rsplit('/', 1)[-1])}")
    print()


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description="Everything about one entity of the memory graph (read-only).")
    ap.add_argument("name")
    ap.add_argument("--agent", help="restrict scoped entities (decisions, questions) to this agent")
    a = ap.parse_args()
    db = cr_config.db_path()
    if not os.path.exists(db):
        print("no index yet:", db)
        return
    cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = find(cx, a.name, a.agent)
    except sqlite3.OperationalError:
        print("graph tables not created yet; run: python graph.py update")
        return
    if not rows:
        print(f"No entity named '{a.name}'. Try the full-text search: python search.py {a.name}")
        return
    for r in rows[:MAX_SHOWN]:
        show(cx, *r)
    if len(rows) > MAX_SHOWN:
        print(f"... {len(rows) - MAX_SHOWN} other entity(ies) with the same name (use --agent).")


if __name__ == "__main__":
    main()

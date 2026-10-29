"""UserPromptSubmit hook: recall what was said elsewhere about the exact identifiers a prompt cites.

Rule: an agent only searches for what it knows it forgot. When a prompt cites an exact anchor
(configured anchor_patterns: decision numbers, question numbers, build numbers, file names...), this
hook prints the person's words, episodes and reports that cite it OUTSIDE the current session, or
in the current session BEFORE its last compaction (what is still in context is never repeated).
Silent when nothing useful is found. Bounded by budgets.recall_chars. No network, no model.

  python recall_hook.py        reads {session_id, prompt, cwd} as JSON on stdin, prints plain text

Cache: <memory_root>/recall_cache.json remembers which anchors were already recalled in a session;
an anchor is recalled again only after a new compaction of that session.

What it never does: open the index in write mode, write anything except its cache under memory_root,
print a secret (the index is already filtered; the output goes through the filter again when
available), fail the prompt (errors go to stderr, exit code 0).
"""
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
from episodes import agent_of, clean, local  # noqa: E402

ANCHORS_MAX = 4
PER_ANCHOR = 2
EXCERPT = 350
CACHE_SESSIONS_MAX = 200


def _parse(ts):
    """ISO timestamp (with 'Z' or an offset) -> aware datetime in UTC."""
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def anchors_in(prompt):
    found = []
    for rx in cr_config.compiled(cr_config.load()["anchor_patterns"]):
        for m in rx.findall(prompt):
            if m and m not in found:
                found.append(m)
    return found


def _scoped_patterns():
    cfg = cr_config.load()
    return [re.compile(cfg["entity_patterns"][t]) for t in cfg.get("entity_scoped_types") or []
            if t in (cfg.get("entity_patterns") or {})]


def _cache_path():
    return os.path.join(cr_config.memory_root(), "recall_cache.json")


def _read_cache():
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_cache(cache):
    while len(cache) > CACHE_SESSIONS_MAX:
        cache.pop(next(iter(cache)))
    tmp = _cache_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, _cache_path())


def recall(prompt, session, agent=""):
    anchors = anchors_in(prompt)
    if not anchors:
        return ""
    db = cr_config.db_path()
    if not os.path.exists(db):
        print("[memory] no index yet: nothing to recall", file=sys.stderr)
        return ""
    cfg = cr_config.load()
    human = cfg.get("human_label") or "human"
    scoped = _scoped_patterns()
    cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    # What precedes the last compaction of THIS session is no longer in context: it may be recalled.
    last_compaction = cx.execute(
        "SELECT max(ts) FROM passages WHERE session = ? AND kind = 'compact_summary'", (session,)).fetchone()[0]
    since_compaction = last_compaction or "9999"
    cache = _read_cache()
    done = cache.setdefault(session or "-", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def already_recalled(anchor):
        """True when the anchor was recalled in this session and no compaction happened since."""
        stamp = done.get(anchor)
        if not stamp:
            return False
        if not last_compaction:
            return True
        try:
            return _parse(stamp) >= _parse(last_compaction)
        except ValueError:
            return False

    lines, seen = [], set()
    for anchor in anchors[:ANCHORS_MAX]:
        if already_recalled(anchor):
            continue
        done[anchor] = now
        is_scoped = any(rx.fullmatch(anchor) for rx in scoped)
        if is_scoped and agent and agent != "unknown":
            # The text of the decision in the project's decisions file, as read by the graph.
            try:
                d = cx.execute("SELECT text FROM entities WHERE type IN ('decision', 'question') AND name = ? "
                               "AND scope = ? AND text IS NOT NULL", (anchor, agent)).fetchone()
            except sqlite3.Error:
                d = None
            if d:
                lines.append(f"- {anchor} - decisions file of {agent}: {' '.join(d[0].split())[:2 * EXCERPT]}")
        before = len(lines)
        # First what was said (person, reports, summaries, episodes); failing that, the last tool call citing it.
        for kinds, limit in (("'human', 'episode', 'agent', 'compact_summary'", PER_ANCHOR), ("'tool'", 1)):
            if len(lines) > before:
                break
            # A decision of one agent is not the same decision for another: scoped anchors and commands
            # are only recalled within the current agent.
            restrict = int((is_scoped or kinds == "'tool'") and bool(agent) and agent != "unknown")
            rows = cx.execute(
                "SELECT p.id, p.ts, p.agent, p.kind, snippet(passages_fts, 0, '\"', '\"', ' ... ', 45) "
                "FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid "
                f"WHERE passages_fts MATCH ? AND p.kind IN ({kinds}) "
                "AND (p.session <> ? OR p.ts < ?) AND p.subagent IS NULL "
                "AND (? = 0 OR p.agent = ?) "
                "ORDER BY p.agent = ? DESC, p.ts DESC LIMIT ?",
                ('"' + anchor.replace('"', '""') + '"', session, since_compaction, restrict, agent, agent, limit))
            for pid, ts, who, kind, excerpt in rows:
                if pid in seen:
                    continue
                seen.add(pid)
                label = {"human": f"{human} said to", "agent": "report of", "compact_summary": "summary of",
                         "tool": "command of"}.get(kind, "episode of")
                lines.append(f"- [{pid}] {anchor} - {label} {who}, {local(ts)}: {' '.join(excerpt.split())[:EXCERPT]}")
    cx.close()
    try:
        _write_cache(cache)
    except OSError:
        pass
    if not lines:
        return ""
    search = os.path.join(HERE, "search.py").replace("\\", "/")
    text = ("[memory] This message cites identifiers already seen elsewhere or before the last compaction. "
            f"Recall (most recent first; details: `python -X utf8 \"{search}\" <word>`):\n" + "\n".join(lines))
    return clean(text)[:int(cfg["budgets"]["recall_chars"])]


def main():
    cr_config.utf8_stdout()
    try:
        data = json.loads(sys.stdin.read() or "{}")
        session = data.get("session_id") or ""
        out = recall(data.get("prompt") or "", session, agent_of(session, data.get("cwd"), data.get("transcript_path")))
        if out:
            print(out)
    except Exception as exc:  # a recall must never block the prompt
        print(f"[memory] recall unavailable: {type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()

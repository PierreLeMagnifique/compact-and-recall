"""Light knowledge graph over the passages index. No model, same SQLite file as the index.

What it does
  Nodes (entities) are recognised by regex in every indexed passage:
    - the generic types of cr_config.load()["entity_patterns"] (decision D12, question Q4,
      version v1172, script name...); types listed in "entity_scoped_types" are scoped per agent,
    - `agent` (names of agents already present in the passages table),
    - `file` and `folder` (paths touched by tool calls and episodes).
  Links:
    - mentions   : passage -> entity (table `mentions`),
    - wrote      : agent -> file, dated (Write / Edit / NotebookEdit tool calls),
    - contains   : folder -> file,
    - replaces / cancels / refines / completes / refers_to : decision -> decision, parsed from the
      decisions markdown of every registered project (English keywords
      `replaces|supersedes|cancels|refines|completes|see`, and `replaced by D12` style phrases),
    - closes     : decision -> question ("Q4 closed by D12"),
    - keeps_decisions_in : agent -> decisions file.
  Decisions are read from <project cwd>/<decisions_file> for every distinct (agent, cwd) of the
  sessions registry written by the SessionStart hook.

How to call it
  python graph.py update                  incremental pass (also called by the indexer at the end of a run)
  python graph.py stats                   counts per entity type and per relation (read-only)
  python graph.py rebuild                 forget the last processed passage and re-run update on everything
  python graph.py ingest-map <file.jsonl> load a hand-written map of entities and links (see ingest_map)
  From Python (indexer): graph.update(cx) on an open connection that already holds the writer lock.

What it never does
  It never writes outside the SQLite file under memory_root. It never prints the text of a passage.
  Map texts go through the secrets filter when it is available. Entity texts that already exist are
  never overwritten by a map (the text of a decision comes from the decisions file).
"""
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cr_config  # noqa: E402

try:
    import secrets_filter  # ported separately; optional here
except ImportError:  # pragma: no cover
    secrets_filter = None

BATCH = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', text TEXT,
  UNIQUE(type, name, scope));
CREATE INDEX IF NOT EXISTS entities_name ON entities(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS mentions (entity INTEGER NOT NULL, passage INTEGER NOT NULL,
  PRIMARY KEY (entity, passage)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mentions_passage ON mentions(passage);
CREATE TABLE IF NOT EXISTS links (source INTEGER NOT NULL, target INTEGER NOT NULL, relation TEXT NOT NULL,
  passage INTEGER, since TEXT, UNIQUE(source, target, relation, passage));
CREATE INDEX IF NOT EXISTS links_target ON links(target, relation);
CREATE TABLE IF NOT EXISTS graph_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS map_entities (map INTEGER NOT NULL, entity INTEGER NOT NULL,
  PRIMARY KEY (map, entity)) WITHOUT ROWID;
"""

WRITING_TOOLS = ("Write", "Edit", "NotebookEdit")


def _clean(text):
    """Secrets filter when available, identity otherwise."""
    if not text or secrets_filter is None:
        return text
    fn = getattr(secrets_filter, "clean", None) or getattr(secrets_filter, "scrub", None)
    return fn(text) if fn else text


def _patterns():
    cfg = cr_config.load()
    pats = [(t, re.compile(rx)) for t, rx in (cfg.get("entity_patterns") or {}).items()]
    scoped = set(cfg.get("entity_scoped_types") or [])
    return pats, scoped


def known_agents(cx):
    """One regex that matches any agent name already seen in the passages (4 chars or more)."""
    names = {r[0] for r in cx.execute("SELECT DISTINCT agent FROM passages WHERE agent IS NOT NULL AND agent <> '?'")}
    names = {n for n in names if len(n) >= 4 and n not in ("episode", "unknown")}
    if not names:
        return None
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\b")


class Graph:
    def __init__(self, cx):
        self.cx = cx
        self.cache = {}

    def entity(self, type_, name, scope="", text=None):
        key = (type_, name, scope)
        if key in self.cache:
            if text:
                self.cx.execute("UPDATE entities SET text = ? WHERE id = ?", (text, self.cache[key]))
            return self.cache[key]
        row = self.cx.execute("SELECT id FROM entities WHERE type = ? AND name = ? AND scope = ?", key).fetchone()
        if row:
            eid = row[0]
            if text:
                self.cx.execute("UPDATE entities SET text = ? WHERE id = ?", (text, eid))
        else:
            eid = self.cx.execute("INSERT INTO entities(type, name, scope, text) VALUES (?, ?, ?, ?)",
                                  (type_, name, scope, text)).lastrowid
        self.cache[key] = eid
        return eid

    def link(self, source, target, relation, passage=0, since=None):
        # passage 0 and not NULL: two NULLs are never equal for UNIQUE, the link would be copied at every pass
        self.cx.execute("INSERT OR IGNORE INTO links VALUES (?, ?, ?, ?, ?)",
                        (source, target, relation, passage or 0, since))


def normalize_path(p):
    p = p.strip().strip("\"'").replace("\\", "/")
    p = re.sub(r"^/([a-zA-Z])/", lambda m: m.group(1).upper() + ":/", p)
    if re.match(r"^[a-zA-Z]:/", p):
        p = p[0].upper() + p[1:]
    return p.rstrip("/")


def process_passages(cx, g, rx_agents):
    """Mentions and file links for every passage after the last one processed (graph_state.last_passage)."""
    pats, scoped = _patterns()
    last = int((cx.execute("SELECT value FROM graph_state WHERE key = 'last_passage'").fetchone() or ["0"])[0])
    total = 0
    while True:
        rows = cx.execute("SELECT id, agent, kind, tool, files, ts, text FROM passages WHERE id > ? ORDER BY id LIMIT ?",
                          (last, BATCH)).fetchall()
        if not rows:
            break
        with cx:
            for pid, agent, kind, tool, files, ts, text in rows:
                text = text or ""
                agent_scope = agent or "?"
                seen = set()
                for type_, rx in pats:
                    for name in set(rx.findall(text)):
                        seen.add((type_, name, agent_scope if type_ in scoped else ""))
                if rx_agents is not None:
                    for name in set(rx_agents.findall(text)):
                        seen.add(("agent", name, ""))
                for type_, name, scope in seen:
                    cx.execute("INSERT OR IGNORE INTO mentions VALUES (?, ?)", (g.entity(type_, name, scope), pid))
                if files and kind in ("tool", "episode"):
                    for f in files.split("\n"):
                        path = normalize_path(f)
                        if len(path) < 4:
                            continue
                        fid = g.entity("file", path)
                        cx.execute("INSERT OR IGNORE INTO mentions VALUES (?, ?)", (fid, pid))
                        if tool in WRITING_TOOLS and agent and agent != "?":
                            g.link(g.entity("agent", agent), fid, "wrote", pid, ts)
                        folder = path.rsplit("/", 1)[0] if "/" in path else ""
                        if folder:
                            g.link(g.entity("folder", folder), fid, "contains")
                total += 1
            last = rows[-1][0]
            cx.execute("INSERT OR REPLACE INTO graph_state VALUES ('last_passage', ?)", (str(last),))
    return total


RX_SECTION = re.compile(r"^#{2,4} (D\d{1,3})\b[^\n]*", re.M)
RX_TABLE_ROW = re.compile(r"^\| *(D\d{1,3}) *\|(.*)$", re.M)
# "replaced by D12", "superseded by D12", "cancelled by D12", "closed by D12": the newer decision replaces this one
RX_REPLACED_BY = re.compile(r"(?:replaced|superseded|cancell?ed|closed)\s+by\s+#?\s*(D\d{1,3})", re.I)
# newer -> older: "replaces D3", "supersedes decision D3", "cancels D3", "refines D3", "completes D3", "see D3"
RX_RELATION = re.compile(r"\b(replaces|supersedes|cancels|refines|completes|see)\s+(?:the\s+)?(?:decision\s+)?(D\d{1,3})",
                         re.I)
RELATION_OF = {"replaces": "replaces", "supersedes": "replaces", "cancels": "cancels", "refines": "refines",
               "completes": "completes", "see": "refers_to"}
RX_Q_CLOSED = re.compile(r"\b(Q\d{1,3}[a-z]?)\s+closed\s+by\s+(D\d{1,3})", re.I)


def decision_files():
    """Distinct (agent, path) pairs from the sessions registry: <cwd>/<decisions_file>."""
    rel = cr_config.load()["decisions_file"]
    out = {}
    for info in cr_config._read_sessions().values():
        agent, cwd = info.get("agent"), info.get("cwd")
        if agent and cwd:
            out[(agent, os.path.join(cwd, rel))] = None
    return list(out)


def _decision_relations(g, did, name, agent, body):
    for word, other in set(RX_RELATION.findall(body)):
        if other != name:
            g.link(did, g.entity("decision", other, agent), RELATION_OF.get(word.lower(), "refers_to"))
    for newer in set(RX_REPLACED_BY.findall(body)) - {name}:
        g.link(g.entity("decision", newer, agent), did, "replaces")


def process_decisions(cx, g):
    """Text of each decision and its relations, from the decisions markdown of every registered project."""
    n = 0
    for agent, path in decision_files():
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        with cx:
            sections = list(RX_SECTION.finditer(text))
            for i, m in enumerate(sections):
                body = text[m.start():sections[i + 1].start() if i + 1 < len(sections) else len(text)]
                did = g.entity("decision", m.group(1), agent, body[:1500])
                _decision_relations(g, did, m.group(1), agent, body)
                n += 1
            for q, d in set(RX_Q_CLOSED.findall(text)):
                g.link(g.entity("decision", d, agent), g.entity("question", q, agent), "closes")
            for m in RX_TABLE_ROW.finditer(text):
                did = g.entity("decision", m.group(1), agent, None)
                if not cx.execute("SELECT text FROM entities WHERE id = ?", (did,)).fetchone()[0]:
                    cx.execute("UPDATE entities SET text = ? WHERE id = ?",
                               (("| " + m.group(1) + " |" + m.group(2))[:1500], did))
                _decision_relations(g, did, m.group(1), agent, m.group(2))
                n += 1
            g.link(g.entity("agent", agent), g.entity("file", normalize_path(path)), "keeps_decisions_in")
    return n


def ingest_map(cx, path):
    """Map provided by hand (one JSON object per line):
         {"entity": {"type": ..., "name": ..., "scope": "", "text": ...}}
         {"link": {"source": [type, name, scope], "target": [type, name, scope], "relation": ..., "since": ...}}
    A map REPLACES its previous version: its links carry a negative passage number owned by the map, deleted then
    rewritten. The text of an entity that already exists is never overwritten. Entities that left the map and have
    no mention and no other link are removed (they only existed through the map)."""
    cx.executescript(SCHEMA)
    map_key = "map:" + os.path.normcase(os.path.abspath(path))
    r = cx.execute("SELECT value FROM graph_state WHERE key = ?", (map_key,)).fetchone()
    if r:
        num = int(r[0])
    else:
        num = min([-1] + [int(v) - 1 for (v,) in cx.execute("SELECT value FROM graph_state WHERE key LIKE 'map:%'")])
    entities, links = [], []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            o = json.loads(line)
            if "entity" in o:
                entities.append(o["entity"])
            elif "link" in o:
                links.append(o["link"])
            else:
                raise ValueError(f"line {i}: neither entity nor link")
    texts = [_clean(e.get("text") or "") for e in entities]
    with cx:
        old = {e for (e,) in cx.execute("SELECT entity FROM map_entities WHERE map = ?", (num,))}
        cx.execute("DELETE FROM links WHERE passage = ?", (num,))
        cx.execute("DELETE FROM map_entities WHERE map = ?", (num,))
        ids = {}
        for e, text in zip(entities, texts):
            key = (e["type"], e["name"], e.get("scope") or "")
            cx.execute("INSERT OR IGNORE INTO entities(type, name, scope, text) VALUES (?, ?, ?, ?)", key + (text or None,))
            eid, previous = cx.execute("SELECT id, text FROM entities WHERE type = ? AND name = ? AND scope = ?",
                                       key).fetchone()
            if not previous and text:
                cx.execute("UPDATE entities SET text = ? WHERE id = ?", (text, eid))
            ids[key] = eid
            cx.execute("INSERT OR IGNORE INTO map_entities VALUES (?, ?)", (num, eid))

        def id_of(triplet):
            key = (triplet[0], triplet[1], triplet[2] if len(triplet) > 2 else "")
            if key not in ids:  # link to an entity not declared in the map: created bare
                cx.execute("INSERT OR IGNORE INTO entities(type, name, scope) VALUES (?, ?, ?)", key)
                ids[key] = cx.execute("SELECT id FROM entities WHERE type = ? AND name = ? AND scope = ?",
                                      key).fetchone()[0]
                cx.execute("INSERT OR IGNORE INTO map_entities VALUES (?, ?)", (num, ids[key]))
            return ids[key]

        for link in links:
            cx.execute("INSERT OR IGNORE INTO links VALUES (?, ?, ?, ?, ?)",
                       (id_of(link["source"]), id_of(link["target"]), link["relation"], num, link.get("since")))
        removed = 0
        for eid in old - set(ids.values()):
            if not cx.execute("SELECT 1 FROM mentions WHERE entity = ? LIMIT 1", (eid,)).fetchone() and not cx.execute(
                    "SELECT 1 FROM links WHERE source = ? OR target = ? LIMIT 1", (eid, eid)).fetchone():
                cx.execute("DELETE FROM entities WHERE id = ?", (eid,))
                removed += 1
        cx.execute("INSERT OR REPLACE INTO graph_state VALUES (?, ?)", (map_key, str(num)))
        cx.execute("INSERT OR REPLACE INTO graph_state VALUES (?, ?)",
                   (map_key + ":updated", time.strftime("%Y-%m-%d %H:%M:%S")))
    return len(ids), len(links), removed, num


def update(cx):
    """Incremental pass. The caller holds the writer lock (the indexer calls this at the end of its run)."""
    cx.executescript(SCHEMA)
    g = Graph(cx)
    n_dec = process_decisions(cx, g)
    n = process_passages(cx, g, known_agents(cx))
    return n, n_dec


# --- writer lock shared with the indexer (same lock file under memory_root) -------------------------------------
def _lock(fh):
    try:
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    except ImportError:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock(fh):
    try:
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _lock_path():
    """The indexer's lock file when the module exposes it, else <memory_root>/indexer.lock."""
    try:
        import indexer
        for name in ("LOCK_PATH", "LOCK_FILE", "LOCK"):
            if isinstance(getattr(indexer, name, None), str):
                return getattr(indexer, name)
    except ImportError:
        pass
    return os.path.join(cr_config.memory_root(), "indexer.lock")


def _open_rw():
    cx = sqlite3.connect(cr_config.db_path())
    cx.execute("PRAGMA journal_mode=WAL")
    return cx


def main():
    cr_config.utf8_stdout()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        if not os.path.exists(cr_config.db_path()):
            print("no index yet:", cr_config.db_path())
            return
        cx = sqlite3.connect(f"file:{cr_config.db_path()}?mode=ro", uri=True)
        try:
            for r in cx.execute("SELECT type, count(*) FROM entities GROUP BY 1 ORDER BY 2 DESC"):
                print("  ", r)
            print("mentions", cx.execute("SELECT count(*) FROM mentions").fetchone()[0],
                  "; links", cx.execute("SELECT relation, count(*) FROM links GROUP BY 1").fetchall())
        except sqlite3.OperationalError:
            print("graph tables not created yet; run: python graph.py update")
        return
    if cmd not in ("update", "rebuild", "ingest-map"):
        print(__doc__)
        return
    lock = open(_lock_path(), "a+")
    _lock(lock)  # waits for the end of an indexer pass: single writer
    try:
        cx = _open_rw()
        if cmd == "ingest-map":
            if len(sys.argv) < 3:
                print("usage: python graph.py ingest-map <file.jsonl>")
                return
            n_e, n_l, removed, num = ingest_map(cx, sys.argv[2])
            print(f"{n_e} entities, {n_l} links ingested (map number {num}); "
                  f"{removed} entity(ies) that left the map removed")
            return
        if cmd == "rebuild":  # after a change of patterns: every passage again (mentions and links without duplicates)
            cx.executescript(SCHEMA)
            with cx:
                cx.execute("DELETE FROM graph_state WHERE key = 'last_passage'")
        n, n_dec = update(cx)
        print(f"{n} passage(s) linked to the graph, {n_dec} decision(s) read")
    finally:
        _unlock(lock)
        lock.close()


if __name__ == "__main__":
    main()

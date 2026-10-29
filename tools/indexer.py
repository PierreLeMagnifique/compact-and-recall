"""Indexer for compact-and-recall: transcripts -> local SQLite full-text index.

Reads the Claude Code transcripts (``transcripts_glob`` in the config), cuts
them into passages, removes secrets (secrets_filter.py) and writes them into
``memory.db`` under memory_root (SQLite, FTS5, WAL mode).

    python indexer.py                 one pass over every transcript (same as --once)
    python indexer.py --loop          one pass every index.interval_minutes, forever
    python indexer.py --file <x.jsonl>  one transcript only
    python indexer.py --stats         sizes and counts
    python indexer.py --rebuild       empty the index first (after a filter change)
    python indexer.py --refilter 12 40  re-run the filter on these passage ids (from check_secrets.py)

Safeguards kept from the original design:
  * single writer: a file lock (msvcrt on Windows, fcntl elsewhere), readers use read-only connections;
  * incremental: every file resumes at its last fully processed byte, never before an unanswered tool call;
  * tool inputs and outputs are truncated (index.max_* in the config), thinking blocks too;
  * the secrets filter runs on every text at write time, and only counts of removals are stored (``removals``);
  * tool calls left without a result (interruption) are written as orphans once the file is idle;
  * sub-agent transcripts (``<session>/subagents/agent-*.jsonl``) are attributed to their parent session and agent;
  * passages that match ``exclude_patterns`` (tool passages only) are never written;
  * low process priority, no model call, nothing leaves the machine.

Public schema (relied upon by search.py, graph.py, episodes.py, verify_gaps.py):
  files(path PK, agent, project, session, is_subagent, parent_session, bytes_done, turn, mtime, last_indexed)
  passages(id, session, agent, project, subagent, turn, ts, kind, tool, files, uuid, text)
  passages_fts (FTS5 on passages.text, unicode61 remove_diacritics 2, kept in sync by triggers)
  episodes(path PK, agent, at, source, bytes, passage)
  removals(kind PK, n)
Kinds: human, instruction, agent, thinking, tool, relay, notification, compact_summary, command, episode.

Exposed for other tools: open_db(), open_db_readonly(), LOCK_PATH, LOG_PATH, log(), acquire_lock() /
release_lock(), Reader, RX_MESSAGE_TYPE, keep(), SCHEMA.

Never writes outside memory_root. Never prints a secret (the log only carries counts and file names).
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import cr_config
import secrets_filter

try:
    import graph
except ImportError:  # the graph module is optional
    graph = None

CFG = cr_config.load()
_IDX = CFG.get('index', {})
MAX_INPUT = int(_IDX.get('max_input_chars', 2000))        # characters kept of a tool call
MAX_OUTPUT_HEAD = int(_IDX.get('max_output_head', 1500))
MAX_OUTPUT_TAIL = int(_IDX.get('max_output_tail', 500))
MAX_THINKING = int(_IDX.get('max_thinking_chars', 4000))
BATCH = int(_IDX.get('batch_passages', 2000))               # passages written per transaction
INTERVAL_MINUTES = float(_IDX.get('interval_minutes', 10))
IDLE_SECONDS = 3600                                          # a file idle this long gets its orphans flushed

LOCK_PATH = os.path.join(cr_config.memory_root(), 'indexer.lock')
LOG_PATH = os.path.join(cr_config.memory_root(), 'indexer.log')

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, agent TEXT, project TEXT, session TEXT,
  is_subagent INTEGER NOT NULL DEFAULT 0, parent_session TEXT,
  bytes_done INTEGER NOT NULL DEFAULT 0, turn INTEGER NOT NULL DEFAULT 0, mtime REAL, last_indexed TEXT);
CREATE TABLE IF NOT EXISTS passages (
  id INTEGER PRIMARY KEY, session TEXT NOT NULL, agent TEXT, project TEXT, subagent TEXT,
  turn INTEGER, ts TEXT, kind TEXT NOT NULL, tool TEXT, files TEXT, uuid TEXT, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS passages_session_turn ON passages(session, turn);
CREATE INDEX IF NOT EXISTS passages_agent_ts ON passages(agent, ts);
CREATE INDEX IF NOT EXISTS passages_kind_ts ON passages(kind, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
  text, content='passages', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
  INSERT INTO passages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS passages_au AFTER UPDATE OF text ON passages BEGIN
  INSERT INTO passages_fts(passages_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO passages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
  INSERT INTO passages_fts(passages_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE TABLE IF NOT EXISTS episodes (
  path TEXT PRIMARY KEY, agent TEXT NOT NULL, at TEXT NOT NULL, source TEXT, bytes INTEGER, passage INTEGER);
CREATE INDEX IF NOT EXISTS episodes_agent_at ON episodes(agent, at);
CREATE TABLE IF NOT EXISTS removals (kind TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""

RX_REMINDER = re.compile(r'<system-reminder>[\s\S]*?</system-reminder>')
# With or without a space: a transcript rewritten by a script (json.dumps) carries "type": "user".
RX_MESSAGE_TYPE = re.compile(rb'"type":\s*"(?:user|assistant)"')
RX_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|~/|/[a-z]/|/(?:home|tmp|var|opt|etc|srv|mnt|data|work)/)[^\s"\'|<>;&`*?]{3,200}')
PATH_KEYS = ('file_path', 'path', 'notebook_path')
RX_SUBAGENT = re.compile(r'[\\/]subagents[\\/](agent-[\w-]+)\.jsonl$')
RX_PARENT_SESSION = re.compile(r'[\\/]([0-9a-fA-F-]{36})[\\/]subagents[\\/]')
# Only the commands the session itself launched count for the agent declaration: text injected at start-up
# or files read from other projects would attribute the session to the wrong agent.
RX_COMMAND = re.compile(r'"command":\s*"((?:[^"\\]|\\.){0,4000})"')
RX_DECLARE = re.compile(CFG['agent_declaration_regex']) if CFG.get('agent_declaration_regex') else None
RELAY_MARKERS = tuple(m for m in (CFG.get('relay_markers') or []) if m)
EXCLUDE = cr_config.compiled(CFG.get('exclude_patterns') or [])
RX_EPISODE = re.compile(r'EPISODE_(\d{4}-\d{2}-\d{2})_(\d{2})h(\d{2})_([a-z_]+?)(?:_\d+)?\.md$')
NATIVE_SUMMARY_SOURCE = 'native_summary'   # episodes imported from compaction summaries already indexed


# ---------- process, lock, log, database ----------

def low_priority():
    """Background priority for CPU and disk; optional, never fails."""
    try:
        if sys.platform == 'win32':
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetCurrentProcess()
            k.SetPriorityClass(h, 0x00100000)  # PROCESS_MODE_BACKGROUND_BEGIN
            k.SetPriorityClass(h, 0x00000040)  # IDLE_PRIORITY_CLASS
        elif hasattr(os, 'nice'):
            os.nice(10)
    except Exception:
        pass


def acquire_lock(fh):
    """Non-blocking exclusive lock on an open file handle. True when acquired (or when no lock API exists)."""
    try:
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except ImportError:
        return True
    except OSError:
        return False


def release_lock(fh):
    try:
        if sys.platform == 'win32':
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg
    print(line, flush=True)
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def open_db():
    """Writer connection (WAL, schema created if needed). Only the indexer should call this."""
    cx = sqlite3.connect(cr_config.db_path())
    cx.execute('PRAGMA journal_mode=WAL')
    cx.execute('PRAGMA synchronous=NORMAL')
    cx.executescript(SCHEMA)
    return cx


def open_db_readonly():
    return sqlite3.connect(f'file:{cr_config.db_path()}?mode=ro', uri=True)


# ---------- which agent wrote this session ----------

def parent_transcript(path):
    """<project>/<session>/subagents/agent-x.jsonl -> <project>/<session>.jsonl."""
    return os.path.dirname(os.path.dirname(path)) + '.jsonl'


def session_of(path):
    """(session id, is_subagent). A sub-agent file belongs to the session named by its parent folder."""
    m = RX_PARENT_SESSION.search(path)
    if m:
        return m.group(1), True
    return os.path.splitext(os.path.basename(path))[0], False


def declared_agent(path):
    """Agent most often declared by the commands the session launched (agent_declaration_regex), first 50 MB."""
    if RX_DECLARE is None:
        return None
    c = Counter()
    with open(path, 'rb') as f:
        block = f.read(50 * 1024 * 1024).decode('utf-8', 'replace')
    for cmd in RX_COMMAND.finditer(block):
        for m in RX_DECLARE.finditer(cmd.group(1)):
            name = m.group(1) if m.groups() else m.group(0)
            if name:
                c[name] += 1
    return c.most_common(1)[0][0] if c else None


def resolve_agent(cx, path, session, is_subagent):
    """Sessions registry first, then (sub-agent) the parent's agent, then the declaration regex, then the
    project folder encoded in the transcript path."""
    info = cr_config.session_info(session)
    if info.get('agent'):
        return info['agent']
    if is_subagent:
        parent = parent_transcript(path)
        r = cx.execute('SELECT agent FROM files WHERE path = ?', (parent,)).fetchone()
        if r and r[0] and r[0] != 'unknown':
            return r[0]
        if os.path.exists(parent):
            found = declared_agent(parent)
            if found:
                return found
    return declared_agent(path) or cr_config.project_of_transcript(path)


# ---------- cutting a transcript into passages ----------

def text_of(content):
    if isinstance(content, str):
        return content
    pieces = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get('type') == 'text':
                pieces.append(b.get('text', ''))
            elif b.get('type') == 'tool_reference' or 'text' in b:
                pieces.append(str(b.get('text', '')))
    return '\n'.join(pieces)


def shorten(t, head, tail=0):
    if len(t) <= head + tail:
        return t
    return t[:head] + f'\n[... {len(t) - head - tail} characters cut ...]\n' + (t[-tail:] if tail else '')


def paths_of(tool_name, inp):
    seen = []
    if isinstance(inp, dict):
        for k in PATH_KEYS:
            if isinstance(inp.get(k), str):
                seen.append(inp[k])
        cmd = inp.get('command')
        if isinstance(cmd, str):
            seen.extend(RX_PATH.findall(cmd)[:10])
    return sorted(set(v.rstrip('.,)') for v in seen))[:15]


def describe_call(name, inp):
    if not isinstance(inp, dict):
        return f'[{name}] {inp}'
    if name in ('Bash', 'PowerShell'):
        body = inp.get('command', '')
    elif name == 'Edit':
        body = f"{inp.get('file_path', '')}\n--- before\n{inp.get('old_string', '')}\n--- after\n{inp.get('new_string', '')}"
    elif name == 'Write':
        body = f"{inp.get('file_path', '')}\n{inp.get('content', '')}"
    else:
        body = json.dumps(inp, ensure_ascii=False)
    return f'[{name}] ' + shorten(body, MAX_INPUT)


def user_kind(o, text):
    """Kind of a user-side message; None for meta lines that carry no content of their own."""
    if o.get('isCompactSummary'):
        return 'compact_summary'
    if o.get('isMeta'):
        return None
    t = text.lstrip()
    if t.startswith('<task-notification'):
        return 'notification'
    if t.startswith('<command-') or t.startswith('<local-command'):
        return 'command'
    if RELAY_MARKERS and any(t.startswith(m) for m in RELAY_MARKERS):
        return 'relay'
    return 'human'


class Reader:
    """Reads a transcript from a given byte offset; produces passages (dicts) and the offset to resume at.

    ``clean`` is the text filter (secrets_filter.clean by default; verify_gaps.py passes an identity to count
    without filtering).
    """

    def __init__(self, path, session, agent, project, turn=0, counts=None, clean=None):
        self.path, self.session, self.agent, self.project = path, session, agent, project
        self.turn = turn
        self.counts = counts if counts is not None else Counter()
        self.clean = clean or secrets_filter.clean
        self.pending = {}   # tool_use_id -> (passage, line offset, turn before the line)
        self.ready = []     # (line offset, turn before the line, passage)
        self.subagent = None
        m = RX_SUBAGENT.search(path)
        if m:
            self.subagent = m.group(1)

    def passage(self, o, kind, text, tool=None, files=None):
        return {
            'session': self.session, 'agent': self.agent, 'project': self.project, 'subagent': self.subagent,
            'turn': self.turn, 'ts': o.get('timestamp'), 'kind': kind, 'tool': tool,
            'files': '\n'.join(files) if files else None, 'uuid': o.get('uuid'),
            'text': self.clean(text, self.counts),
        }

    def line(self, offset, o):
        t = o.get('type')
        if t not in ('user', 'assistant'):
            return
        turn_before = self.turn
        msg = o.get('message') or {}
        content = msg.get('content')
        if t == 'user':
            if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                for b in content:
                    if not (isinstance(b, dict) and b.get('type') == 'tool_result'):
                        continue
                    waiting = self.pending.pop(b.get('tool_use_id'), None)
                    if waiting is None:
                        continue
                    p, call_offset, call_turn = waiting
                    out = text_of(b.get('content'))
                    p['text'] += '\n-> ' + self.clean(shorten(out, MAX_OUTPUT_HEAD, MAX_OUTPUT_TAIL), self.counts)
                    if b.get('is_error'):
                        p['text'] += '\n[error]'
                    self.ready.append((call_offset, call_turn, p))
                return
            text = RX_REMINDER.sub('', text_of(content)).strip()
            if not text:
                return
            kind = user_kind(o, text)
            if kind is None:
                return
            if kind == 'human' and self.subagent:
                # Inside a sub-agent the "user message" is the prompt written by the parent agent, never the person.
                kind = 'instruction'
            if kind in ('human', 'instruction'):
                self.turn += 1
                self.flush_orphans()
            self.ready.append((offset, turn_before, self.passage(o, kind, text)))
            return
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'text' and b.get('text', '').strip():
                self.ready.append((offset, turn_before, self.passage(o, 'agent', b['text'])))
            elif b.get('type') == 'thinking' and b.get('thinking', '').strip():
                self.ready.append((offset, turn_before, self.passage(o, 'thinking', shorten(b['thinking'], MAX_THINKING))))
            elif b.get('type') == 'tool_use':
                name, inp = b.get('name', '?'), b.get('input')
                p = self.passage(o, 'tool', describe_call(name, inp), name, paths_of(name, inp))
                self.pending[b.get('id')] = (p, offset, turn_before)

    def flush_orphans(self):
        """Calls left without a result (interruption): written as they are, otherwise the file would stay stuck."""
        for p, call_offset, call_turn in self.pending.values():
            p['text'] += '\n[no result]'
            self.ready.append((call_offset, call_turn, p))
        self.pending.clear()

    def resume_point(self, end):
        """Offset and turn to resume at: before the first tool call still waiting for its result."""
        if not self.pending:
            return end, self.turn
        offset, turn = min((o, t) for _, o, t in self.pending.values())
        return offset, turn


def keep(p):
    """Exclusion applies to tool passages only (they carry file contents); messages and summaries that merely
    cite an excluded name are kept, otherwise whole compaction summaries would be lost."""
    if not EXCLUDE or p['kind'] != 'tool':
        return True
    hay = p['text'] + '\n' + (p['files'] or '')
    return not any(rx.search(hay) for rx in EXCLUDE)


def write_batch(cx, reader, offset_read, meta):
    cut, turn = reader.resume_point(offset_read)
    rows = [p for (o, _, p) in reader.ready if o < cut and keep(p)]
    reader.ready = [x for x in reader.ready if x[0] >= cut]
    with cx:
        cx.executemany(
            'INSERT INTO passages(session, agent, project, subagent, turn, ts, kind, tool, files, uuid, text) '
            'VALUES (:session, :agent, :project, :subagent, :turn, :ts, :kind, :tool, :files, :uuid, :text)',
            sorted(rows, key=lambda p: p['ts'] or ''))
        cx.execute(
            'INSERT INTO files(path, agent, project, session, is_subagent, parent_session, bytes_done, turn, mtime, last_indexed) '
            'VALUES (:path, :agent, :project, :session, :is_subagent, :parent_session, :bytes_done, :turn, :mtime, :last_indexed) '
            'ON CONFLICT(path) DO UPDATE SET bytes_done=excluded.bytes_done, turn=excluded.turn, agent=excluded.agent, '
            'mtime=excluded.mtime, last_indexed=excluded.last_indexed',
            dict(meta, bytes_done=cut, turn=turn, last_indexed=time.strftime('%Y-%m-%d %H:%M:%S')))
        for kind, n in reader.counts.items():
            cx.execute('INSERT INTO removals(kind, n) VALUES (?, ?) ON CONFLICT(kind) DO UPDATE SET n = n + excluded.n',
                       (kind, n))
        reader.counts.clear()
    return len(rows)


def index_file(cx, path, counts):
    size = os.path.getsize(path)
    row = cx.execute('SELECT bytes_done, turn, agent FROM files WHERE path = ?', (path,)).fetchone()
    start, turn, agent = row if row else (0, 0, None)
    if size <= start:
        return 0
    session, is_subagent = session_of(path)
    project = cr_config.project_of_transcript(path)
    if agent is None:
        agent = resolve_agent(cx, path, session, is_subagent)
    meta = {'path': path, 'agent': agent, 'project': project, 'session': session,
            'is_subagent': int(is_subagent), 'parent_session': session if is_subagent else None,
            'mtime': os.path.getmtime(path)}
    reader = Reader(path, session, agent, project, turn, counts)
    written = 0
    with open(path, 'rb') as f:
        f.seek(start)
        offset = start
        for raw in f:
            if not raw.endswith(b'\n'):
                break  # line still being written
            end_of_line = offset + len(raw)
            if RX_MESSAGE_TYPE.search(raw):
                try:
                    reader.line(offset, json.loads(raw))
                except ValueError:
                    pass
            offset = end_of_line
            if len(reader.ready) >= BATCH and not reader.pending:
                written += write_batch(cx, reader, offset, meta)
    if reader.pending and time.time() - os.path.getmtime(path) > IDLE_SECONDS:
        reader.flush_orphans()  # idle for an hour: no result will come any more
    written += write_batch(cx, reader, offset, meta)
    return written


# ---------- episodes, corrections, statistics ----------

def index_episodes(cx):
    """Frozen episodes (<memory_root>/<episodes.dir>/<agent>/EPISODE_*.md): one row in ``episodes`` and their
    text as a passage of kind 'episode' (except native summaries, already indexed as 'compact_summary')."""
    known = {r[0] for r in cx.execute('SELECT path FROM episodes')}
    added = 0
    with cx:
        for path in sorted(glob.glob(os.path.join(cr_config.episodes_dir(), '*', 'EPISODE_*.md'))):
            path = os.path.abspath(path)
            m = RX_EPISODE.search(path)
            if path in known or not m:
                continue
            agent = os.path.basename(os.path.dirname(path))
            at, source = f'{m.group(1)} {m.group(2)}:{m.group(3)}', m.group(4)
            passage = None
            if source != NATIVE_SUMMARY_SOURCE:
                with open(path, encoding='utf-8') as f:
                    text = f.read()
                ts = datetime.strptime(at, '%Y-%m-%d %H:%M').astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:00.000Z')
                passage = cx.execute(
                    "INSERT INTO passages(session, agent, kind, ts, files, text) VALUES ('episode', ?, 'episode', ?, ?, ?)",
                    (agent, ts, path, secrets_filter.clean(text))).lastrowid
            cx.execute('INSERT INTO episodes(path, agent, at, source, bytes, passage) VALUES (?, ?, ?, ?, ?, ?)',
                       (path, agent, at, source, os.path.getsize(path), passage))
            added += 1
    return added


def reattach(cx):
    """Apply the sessions registry to passages already indexed under another agent, and make sure sub-agent
    prompts are never stored as human words."""
    sessions = cr_config._read_sessions()
    with cx:
        for session, info in sessions.items():
            agent = (info or {}).get('agent')
            if not agent:
                continue
            cx.execute('UPDATE passages SET agent = ? WHERE session = ? AND agent IS NOT ?', (agent, session, agent))
            cx.execute('UPDATE files SET agent = ? WHERE session = ? AND agent IS NOT ?', (agent, session, agent))
        cx.execute("UPDATE passages SET kind = 'instruction' WHERE kind = 'human' AND subagent IS NOT NULL")


def stats(cx):
    db = cr_config.db_path()
    n = cx.execute('SELECT count(*) FROM passages').fetchone()[0]
    print(f'database {db}: {os.path.getsize(db) / 1e6:.1f} MB, {n} passages')
    for r in cx.execute('SELECT agent, kind, count(*) FROM passages GROUP BY 1, 2 ORDER BY 1, 3 DESC'):
        print('  ', r)
    print('filter removals:', dict(cx.execute('SELECT kind, n FROM removals').fetchall()))
    for r in cx.execute('SELECT path, agent, bytes_done, turn FROM files ORDER BY bytes_done DESC LIMIT 10'):
        print('  ', r)


# ---------- one pass ----------

def transcript_files(only=None):
    if only:
        return [os.path.abspath(only)]
    found = glob.glob(CFG['transcripts_glob'], recursive=True)
    return sorted((os.path.abspath(p) for p in found if os.path.isfile(p)), key=os.path.getmtime)


def run_pass(cx, only=None):
    t0 = time.time()
    files = transcript_files(only)
    total, counts = 0, Counter()
    for path in files:
        try:
            total += index_file(cx, path, counts)
        except Exception as e:  # one unreadable file must not block the others
            log(f'ERROR {os.path.basename(path)}: {type(e).__name__} {e}')
    try:
        reattach(cx)
    except Exception as e:
        log(f'ERROR reattach: {type(e).__name__} {e}')
    try:
        n_ep = index_episodes(cx)
    except Exception as e:
        n_ep = 0
        log(f'ERROR episodes: {type(e).__name__} {e}')
    n_graph = 0
    if graph is not None:
        try:
            res = graph.update(cx)
            if isinstance(res, int):
                n_graph = res
            elif isinstance(res, (tuple, list)) and res and isinstance(res[0], int):
                n_graph = res[0]
        except Exception as e:
            log(f'ERROR graph: {type(e).__name__} {e}')
    log(f'{total} passages added, {len(files)} files seen, {n_ep} episodes, {n_graph} linked in the graph, '
        f'{time.time() - t0:.0f} s')
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--file', help='index this transcript only')
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--rebuild', action='store_true', help='empty the index before indexing (after a filter change)')
    ap.add_argument('--refilter', nargs='+', type=int, metavar='ID',
                    help='re-run the secrets filter on these passages (ids given by check_secrets.py)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true', help='one pass (default)')
    mode.add_argument('--loop', action='store_true', help='one pass every index.interval_minutes, until killed')
    a = ap.parse_args()
    cr_config.utf8_stdout()
    cx = open_db()
    if a.stats:
        stats(cx)
        return
    low_priority()
    lock = open(LOCK_PATH, 'a+')
    if not acquire_lock(lock):
        log('another indexer is running: leaving')
        return
    try:
        if a.refilter:
            counts = Counter()
            with cx:
                for pid in a.refilter:
                    row = cx.execute('SELECT text FROM passages WHERE id = ?', (pid,)).fetchone()
                    if row:
                        cx.execute('UPDATE passages SET text = ? WHERE id = ?', (secrets_filter.clean(row[0], counts), pid))
                for kind, n in counts.items():
                    cx.execute('INSERT INTO removals(kind, n) VALUES (?, ?) ON CONFLICT(kind) DO UPDATE SET n = n + excluded.n',
                               (kind, n))
            log(f'{len(a.refilter)} passage(s) refiltered: {dict(counts)}')
            return
        if a.rebuild:
            with cx:
                cx.execute('DELETE FROM passages')
                cx.execute("INSERT INTO passages_fts(passages_fts) VALUES('delete-all')")
                cx.execute('DELETE FROM files')
                cx.execute('DELETE FROM episodes')
                cx.execute('DELETE FROM removals')
            log('index emptied for a rebuild')
        if a.loop:
            log(f'loop mode: one pass every {INTERVAL_MINUTES:g} min')
            while True:
                run_pass(cx, a.file)
                time.sleep(INTERVAL_MINUTES * 60)
        else:
            run_pass(cx, a.file)
    finally:
        release_lock(lock)
        lock.close()


if __name__ == '__main__':
    main()

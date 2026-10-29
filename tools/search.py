"""Full-text search in the compact-and-recall index. Read-only.

    python search.py <words...> [--agent X] [--since 2026-10-20] [--before "2026-10-27 22:00"]
                     [--kind human] [--any] [--recent] [--limit 10]
    python search.py --turn <session> <n>     one whole turn, in order

Each word is searched as it is (a path, an id, D72, planner.py); all words must be present unless
``--any``. When no passage holds every word, the fallback shows the passages that hold the most of them.
Kinds: human, instruction (prompt given to a sub-agent), agent, thinking, tool, relay, notification,
compact_summary, command, episode.
Output: ``[id] date agent kind : excerpt`` (date in local time, timestamps stored in UTC).

Opens the database read-only (``file:...?mode=ro``); never writes, never prints more than the stored
(already filtered) excerpt.
"""
import argparse
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone

import cr_config


def open_db():
    return sqlite3.connect(f'file:{cr_config.db_path()}?mode=ro', uri=True)


def local_time(ts):
    if not ts:
        return '?'
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return ts[:16]


def to_utc(text):
    """'2026-10-27 22:00' or '2026-10-27' is local machine time; a 'T' means already UTC like the transcripts."""
    if 'T' in text:
        return text
    text = text.replace('h', ':').strip()
    d = datetime.strptime(text, '%Y-%m-%d %H:%M' if ':' in text else '%Y-%m-%d')
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def fts_query(words, any_word):
    terms = ['"' + w.replace('"', '""') + '"' for w in words if w.strip()]
    return (' OR ' if any_word else ' AND ').join(terms)


def fold(t):
    return ''.join(c for c in unicodedata.normalize('NFD', t.lower()) if not unicodedata.combining(c))


def by_words_found(cx, rows, words, n):
    """Fallback: passages holding the most words of the question first, then relevance (bm25)."""
    wanted = [fold(w) for w in words]
    texts = dict(cx.execute(f'SELECT id, text FROM passages WHERE id IN ({",".join("?" * len(rows))})',
                            [r[0] for r in rows])) if rows else {}
    count = {r[0]: sum(w in fold(texts.get(r[0], '')) for w in wanted) for r in rows}
    return sorted(rows, key=lambda r: -count[r[0]])[:n], count


def search(cx, words, agent=None, since=None, kind=None, any_word=False, n=10, recent=False, before=None):
    sql = ('SELECT p.id, p.ts, p.agent, p.kind, p.tool, p.session, p.turn, '
           "snippet(passages_fts, 0, '<<', '>>', ' ... ', 40) "
           'FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid WHERE passages_fts MATCH ?')
    args = [fts_query(words, any_word)]
    if agent:
        sql += ' AND p.agent = ?'
        args.append(agent)
    if since:
        sql += ' AND p.ts >= ?'
        args.append(since)
    if before:
        # For a blind benchmark: nothing written after this instant (UTC, as the transcripts).
        sql += ' AND p.ts < ?'
        args.append(before)
    if kind:
        sql += ' AND p.kind = ?'
        args.append(kind)
    sql += ' ORDER BY ' + ('p.ts DESC' if recent else 'bm25(passages_fts)') + ' LIMIT ?'
    args.append(n)
    return cx.execute(sql, args).fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('words', nargs='*')
    ap.add_argument('--agent')
    ap.add_argument('--since', help="'2026-10-20' or '2026-10-20 14:00' (local time) or 2026-10-20T12:00 (UTC)")
    ap.add_argument('--before', help='exclude anything later than this instant (same formats as --since)')
    ap.add_argument('--kind')
    ap.add_argument('--phrase', action='store_true', help='a quoted argument stays one exact phrase')
    ap.add_argument('--any', action='store_true', help='any of the words instead of all of them')
    ap.add_argument('--limit', '-n', type=int, default=10)
    ap.add_argument('--recent', action='store_true', help='newest first instead of most relevant first')
    ap.add_argument('--turn', nargs=2, metavar=('SESSION', 'N'))
    a = ap.parse_intermixed_args()  # options before or after the words
    cr_config.utf8_stdout()
    cx = open_db()
    if a.turn:
        for ts, kind, tool, text in cx.execute(
                'SELECT ts, kind, tool, text FROM passages WHERE session = ? AND turn = ? ORDER BY ts, id',
                (a.turn[0], int(a.turn[1]))):
            print(f'--- {local_time(ts)} {kind} {tool or ""}\n{text}\n')
        return
    if not a.words:
        ap.print_help()
        return
    # Unless --phrase, a quoted argument with several words is split: each word counts on its own, so that
    # the fallback can rank partial matches.
    words = a.words if a.phrase else [w for arg in a.words for w in arg.split()]
    since = to_utc(a.since) if a.since else None
    before = to_utc(a.before) if a.before else None
    rows = search(cx, words, a.agent, since, a.kind, a.any, a.limit, a.recent, before)
    count = {}
    if not rows and not a.any and len(words) > 1:
        rows, count = by_words_found(cx, search(cx, words, a.agent, since, a.kind, True, 200, False, before),
                                     words, a.limit)
        print(f'0 passages with all {len(words)} words; here are those with the most (words found in brackets):')
    if not rows:
        print('0 passages.')
    for pid, ts, agent, kind, tool, session, turn, excerpt in rows:
        found = f' [{count[pid]}/{len(words)} words]' if pid in count else ''
        print(f'[{pid}] {local_time(ts)} {agent} {kind}{" " + tool if tool else ""} '
              f'(session {session[:8]}, turn {turn}){found} :')
        print('   ' + excerpt.replace('\n', ' ')[:600])


if __name__ == '__main__':
    main()

"""Check an indexed session for gaps. Read-only.

Re-reads a whole transcript in memory (no secrets filter, nothing written) and
compares, day by day, the passages it yields with the passages stored for the
same session (sub-agents excluded). A day where the index holds clearly fewer
passages than the transcript is flagged as a gap; fix with
``indexer.py --rebuild`` or by deleting the ``files`` row of that transcript.

    python verify_gaps.py <session> [<session>...] [--days 10]

Opens the database read-only and never prints transcript content, only counts.
"""
import argparse
import glob
import json
import os
import sqlite3
from collections import Counter
from datetime import date, timedelta

import cr_config
import indexer


def transcript_of(session):
    """The main transcript of a session (not a sub-agent file), the largest one if several match."""
    name = session + '.jsonl'
    found = [p for p in glob.glob(cr_config.load()['transcripts_glob'], recursive=True)
             if os.path.basename(p) == name and not indexer.RX_SUBAGENT.search(p)]
    return os.path.abspath(max(found, key=os.path.getsize)) if found else None


def expected(path):
    """Passages per day that the indexer would produce for this file (counting only, identity filter)."""
    reader = indexer.Reader(path, 's', 'a', 'p', 0, Counter(), clean=lambda t, c=None: t)
    offset = 0
    with open(path, 'rb') as f:
        for raw in f:
            if indexer.RX_MESSAGE_TYPE.search(raw):
                try:
                    reader.line(offset, json.loads(raw))
                except ValueError:
                    pass
            offset += len(raw)
    reader.flush_orphans()
    return Counter((p['ts'] or '')[:10] for _, _, p in reader.ready if indexer.keep(p))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sessions', nargs='+')
    ap.add_argument('--days', type=int, default=10, help='how many days back from today (default 10)')
    a = ap.parse_args()
    cr_config.utf8_stdout()
    cx = sqlite3.connect(f'file:{cr_config.db_path()}?mode=ro', uri=True)
    first = date.today() - timedelta(days=a.days - 1)
    for session in a.sessions:
        path = transcript_of(session)
        if not path:
            print(f'{session[:8]}: transcript not found')
            continue
        agent = (cx.execute('SELECT agent FROM files WHERE path = ?', (path,)).fetchone() or ['not indexed'])[0]
        stored = Counter(dict(cx.execute(
            'SELECT substr(ts, 1, 10), count(*) FROM passages WHERE session = ? AND subagent IS NULL '
            "AND kind <> 'episode' GROUP BY 1", (session,))))
        read = expected(path)
        print(f'{agent} ({session[:8]}, {os.path.getsize(path) / 2**20:.1f} MB)')
        days = sorted(set(read) | set(stored) | {(first + timedelta(days=i)).isoformat() for i in range(a.days)})
        gaps = 0
        for day in days:
            if not read[day] and not stored[day]:
                continue
            if day < first.isoformat() and not (read[day] or stored[day]):
                continue
            diff = read[day] - stored[day]
            mark = '  <-- GAP' if diff > max(5, read[day] // 50) else ''
            gaps += bool(mark)
            print(f'   {day}  transcript {read[day]:>6}  index {stored[day]:>6}{mark}')
        print(f'   => {gaps} day(s) to fill' if gaps else '   => no gap')


if __name__ == '__main__':
    main()

"""Audit of the index for leftover secrets. Read-only.

Re-runs the filter patterns on every stored passage and reports, per class,
the number of residues and the passage ids. It never prints a passage: a
reviewer gets numbers, never text. Fix with ``indexer.py --refilter <ids>``.

    python check_secrets.py               all passages
    python check_secrets.py --ids 182 1471

Exit code 1 when at least one residue is found. Opens the database in
read-only mode (``file:...?mode=ro``) and never writes anything.
"""
import argparse
import sqlite3
import sys
from collections import Counter, defaultdict

import cr_config
import secrets_filter


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ids', nargs='*', type=int, help='only these passage ids')
    a = ap.parse_args()
    cr_config.utf8_stdout()
    cx = sqlite3.connect(f'file:{cr_config.db_path()}?mode=ro', uri=True)
    if a.ids:
        rows = cx.execute(f'SELECT id, text FROM passages WHERE id IN ({",".join("?" * len(a.ids))})', a.ids)
    else:
        rows = cx.execute('SELECT id, text FROM passages')
    total, seen, ids = Counter(), 0, defaultdict(list)
    for pid, text in rows:
        seen += 1
        for kind, n in secrets_filter.residues(text).items():
            total[kind] += n
            ids[kind].append(pid)
    print(f'{seen} passages checked.')
    if not total:
        print('No residue.')
    for kind, n in total.most_common():
        lst = ids[kind]
        print(f'{kind}: {n} residue(s) in {len(lst)} passage(s): {" ".join(map(str, lst[:50]))}'
              f'{" ..." if len(lst) > 50 else ""}')
    sys.exit(1 if total else 0)


if __name__ == '__main__':
    main()

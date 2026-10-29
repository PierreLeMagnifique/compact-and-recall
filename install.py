"""Install the compact-and-recall hooks into Claude Code's user settings. Run it yourself, once.

    python install.py --dry-run                 show the exact change, write nothing
    python install.py                           install (asks for confirmation)
    python install.py --yes --task              install and create the Windows scheduled task for the indexer
    python install.py --compact-window 200000   compaction threshold in tokens (default 200000)
    python install.py --keep-compact-window     do not touch autoCompactWindow

What it changes in ~/.claude/settings.json (override with --settings PATH), and nothing else:
  1. hooks.SessionStart   (matcher startup|resume|clear|compact) -> tools/briefing.py --hook   (timeout 15 s)
  2. hooks.PreCompact                                            -> tools/episodes.py hook     (timeout 30 s)
  3. hooks.UserPromptSubmit                                      -> tools/recall_hook.py       (timeout 10 s)
  4. autoCompactWindow -> --compact-window, unless --keep-compact-window
Hook commands are built at run time from this repo's location and the current Python interpreter.
Every other key (permissions, env, model...) is left strictly untouched, and this is asserted after
the write. A dated backup (settings.json.before-compact-and-recall-<date>) is taken first; the file
is re-read after writing; on any problem the backup is restored. Nothing is added twice.

Scheduled indexing: --task creates a Windows task (schtasks) running tools/indexer.py --once every
--task-minutes minutes with pythonw when present; on POSIX the crontab line is printed, never
installed. To undo: copy the backup back, and `schtasks /Delete /TN "compact-and-recall index" /F`.
"""
import argparse
import copy
import datetime
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(REPO, "tools")
sys.path.insert(0, TOOLS)
import cr_config  # noqa: E402

TASK_NAME = "compact-and-recall index"
MANAGED_KEYS = ("hooks", "autoCompactWindow")


def _q(path):
    return '"' + path.replace("\\", "/") + '"'


def hook_specs():
    """(event, matcher, command, timeout) for the three hooks, with absolute paths computed now."""
    py = _q(sys.executable)
    return [
        ("SessionStart", "startup|resume|clear|compact", f"{py} -X utf8 {_q(os.path.join(TOOLS, 'briefing.py'))} --hook", 15),
        ("PreCompact", None, f"{py} -X utf8 {_q(os.path.join(TOOLS, 'episodes.py'))} hook", 30),
        ("UserPromptSubmit", None, f"{py} -X utf8 {_q(os.path.join(TOOLS, 'recall_hook.py'))}", 10),
    ]


def has_command(settings, event, command):
    return any(h.get("command") == command
               for g in (settings.get("hooks") or {}).get(event, []) for h in g.get("hooks", []))


def plan(settings, compact_window, keep_window):
    """Return (new settings, list of change descriptions). Pure: does not touch files."""
    new = copy.deepcopy(settings)
    changes = []
    hooks = new.setdefault("hooks", {})
    for event, matcher, command, timeout in hook_specs():
        groups = hooks.setdefault(event, [])
        if has_command(new, event, command):
            changes.append(f"{event}: already present")
            continue
        target = next((g for g in groups if g.get("matcher") == matcher), None)
        if target is None:
            target = {"hooks": []}
            if matcher:
                target["matcher"] = matcher
            groups.append(target)
        target["hooks"].append({"type": "command", "command": command, "timeout": timeout})
        changes.append(f"{event}: add `{command}` (timeout {timeout} s)")
    if keep_window:
        changes.append(f"autoCompactWindow: kept ({settings.get('autoCompactWindow', 'absent')})")
    else:
        changes.append(f"autoCompactWindow: {settings.get('autoCompactWindow', 'absent')} -> {compact_window}")
        new["autoCompactWindow"] = compact_window
    for model, ms in (settings.get("modelSettings") or {}).items():
        if isinstance(ms, dict) and "autoCompactWindow" in ms:
            changes.append(f"note: modelSettings.{model} keeps its own autoCompactWindow ({ms['autoCompactWindow']})")
    return new, changes


def others(settings):
    return {k: v for k, v in settings.items() if k not in MANAGED_KEYS}


def verify(old, new_on_disk, keep_window, compact_window):
    """Raise AssertionError unless the written file is exactly old + our additions."""
    for event, _, command, _ in hook_specs():
        assert has_command(new_on_disk, event, command), f"hook missing after write: {event}"
    assert others(old) == others(new_on_disk), "a key other than hooks/autoCompactWindow changed"
    for event, groups in (old.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                assert has_command(new_on_disk, event, h.get("command")), f"a pre-existing hook was lost: {event}"
    if keep_window:
        assert old.get("autoCompactWindow") == new_on_disk.get("autoCompactWindow"), "autoCompactWindow changed"
    else:
        assert new_on_disk.get("autoCompactWindow") == compact_window, "autoCompactWindow not applied"


def schedule(minutes, dry_run):
    indexer = os.path.join(TOOLS, "indexer.py")
    if sys.platform == "win32":
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        py = pythonw if os.path.exists(pythonw) else sys.executable
        tr = f"{_q(py)} -X utf8 {_q(indexer)} --once"
        cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", str(minutes), "/TR", tr, "/F"]
        if dry_run:
            print("Scheduled task (not created, dry run): " + " ".join(cmd))
            return
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        print("Scheduled task: " + (r.stdout or r.stderr).strip())
    else:
        print("Add this line to your crontab (crontab -e); nothing is installed for you:")
        print(f"*/{minutes} * * * * {_q(sys.executable)} -X utf8 {_q(indexer)} --once >> {_q(os.path.join(cr_config.memory_root(), 'cron.log'))} 2>&1")


def main():
    cr_config.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--settings", default=os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
                    help="settings file to edit (default: ~/.claude/settings.json)")
    ap.add_argument("--compact-window", type=int, default=200000, help="autoCompactWindow in tokens (default 200000)")
    ap.add_argument("--keep-compact-window", action="store_true", help="leave autoCompactWindow as it is")
    ap.add_argument("--dry-run", action="store_true", help="print the exact change, write nothing")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--task", action="store_true", help="also schedule tools/indexer.py (schtasks on Windows, crontab line on POSIX)")
    ap.add_argument("--task-minutes", type=int, default=int(cr_config.load()["index"]["interval_minutes"]),
                    help="interval of the scheduled indexing in minutes")
    a = ap.parse_args()
    if not a.keep_compact_window and not 20000 <= a.compact_window <= 1000000:
        raise SystemExit("--compact-window must be between 20000 and 1000000 tokens.")

    if os.path.exists(a.settings):
        with open(a.settings, encoding="utf-8") as f:
            try:
                old = json.load(f)
            except ValueError as exc:
                raise SystemExit(f"{a.settings} is not valid JSON ({exc}); fix it first, nothing was done.")
    else:
        old = {}
    if not isinstance(old, dict):
        raise SystemExit(f"{a.settings} does not hold a JSON object; nothing was done.")
    new, changes = plan(old, a.compact_window, a.keep_compact_window)

    print(f"Settings file: {a.settings}" + ("" if os.path.exists(a.settings) else " (will be created)"))
    print("Planned changes:")
    for c in changes:
        print("  - " + c)
    if a.dry_run:
        print("\nResulting managed keys (dry run, nothing written):")
        print(json.dumps({k: new.get(k) for k in MANAGED_KEYS if k in new}, ensure_ascii=False, indent=2))
        if a.task:
            schedule(a.task_minutes, dry_run=True)
        return
    if new == old:
        print("Nothing to change.")
    else:
        if not a.yes:
            try:
                answer = input("Type YES to write these changes: ")
            except EOFError:
                answer = ""
            if answer.strip().upper() != "YES":
                raise SystemExit("Nothing was done.")
        backup = None
        if os.path.exists(a.settings):
            backup = a.settings + ".before-compact-and-recall-" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(a.settings, backup)
            print("Backup: " + backup)
        else:
            os.makedirs(os.path.dirname(a.settings) or ".", exist_ok=True)
        try:
            with open(a.settings, "w", encoding="utf-8") as f:
                json.dump(new, f, ensure_ascii=False, indent=2)
                f.write("\n")
            with open(a.settings, encoding="utf-8") as f:
                reread = json.load(f)
            verify(old, reread, a.keep_compact_window, a.compact_window)
        except Exception as exc:
            if backup:
                shutil.copy2(backup, a.settings)
                raise SystemExit(f"Problem ({exc}): the backup was restored, nothing changed.")
            if os.path.exists(a.settings):
                os.remove(a.settings)
            raise SystemExit(f"Problem ({exc}): the new file was removed, nothing changed.")
        print("Settings written, re-read and verified.")
    if a.task:
        schedule(a.task_minutes, dry_run=False)
    print("\nDone. Sessions pick the hooks up at their next start or next compaction.")


if __name__ == "__main__":
    main()

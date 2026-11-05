# compact-and-recall

Compact early, remember everything: a local, no-service memory for long-running Claude Code
agents, with the measurements that justify it.

Built for a fleet of 30-40 agents on one machine that was spending most of its budget re-reading
giant contexts. We set the compaction window to 200k tokens instead of fighting compaction, and
gave every agent three ways back to the past: an index of every conversation, frozen episodes
written at each compaction, and a short briefing pushed at every start. Measured over two days
on real sessions: **cost per call divided by 2.3, context per call divided by 3, one night's
spend from 134.8M to 39.4M weighted tokens.** Speed per step did not change. "Smarter" we could
not measure. Details and method in [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md).

Python 3.9+, standard library only, one SQLite file, three hooks, one scheduled task. No vector
store, no LLM in the loop, nothing leaves the machine.

## What it does

```
 Live conversation (<= 200k tokens)                    Claude Code hooks
   briefing at start  <----------------------------------  briefing.py      (SessionStart)
   3-5 passages per prompt  <----------------------------  recall_hook.py   (UserPromptSubmit)
   frozen episode written  ----------------------------->  episodes.py      (PreCompact)
 -----------------------------------------------------------------------------------------
 memory.db (SQLite FTS5)  <--- indexer.py every 10 min <--- ~/.claude/projects/**/*.jsonl
   passages of every session, secrets removed; light graph of decisions, files, versions
   search.py / entity.py / measures/*.py read it
```

1. **Early compaction.** `install.py` sets `autoCompactWindow` to 200k. The same agent continues
   in the same conversation; only the summary changes.
2. **Frozen episodes.** At PreCompact, one markdown file per compaction: the human's exact words,
   decisions and reasons, the exact state of the work, files touched. Never rewritten.
3. **Index of everything.** A scheduled indexer reads the transcripts incrementally (byte
   offsets), splits them by turn, filters secrets, and writes to SQLite FTS5 with BM25 ranking.
   Subagent transcripts are attributed to their parent session.
4. **Briefing at start.** At SessionStart (startup, resume, compact): the last five human messages
   verbatim, the standing decisions of the project, the three latest episodes, the search command.
   Around 3k tokens.
5. **Recall at each prompt.** Exact anchors in the prompt (decision numbers, versions, file names)
   are looked up in the index; 3 to 5 passages come back, under 1k tokens, with a cache.
6. **Search on demand.** `search.py`, `entity.py` (everything about one entity in the light graph),
   `graph.py`.
7. **Measures.** `measures/` reads the transcripts and the index and prints cost per call, context
   per call, compactions, subagent share, use of the memory at resumption, speed per step.

The tools are half of it. The other half is how the agent resumes after a compaction:
[docs/OPERATING_RULES.md](docs/OPERATING_RULES.md) is the text we give every agent.

## Install

```
git clone https://github.com/PierreLeMagnifique/compact-and-recall
cd compact-and-recall
cp config.example.json config.json          # optional; defaults work
python tools/cr_config.py                    # shows which config file is used and where the memory will live
python install.py --dry-run                  # shows the settings.json change, writes nothing
python install.py                            # dated backup, adds the hooks, sets autoCompactWindow
python install.py --task                     # Windows: scheduled task every 10 min; POSIX: prints a crontab line
python tools/indexer.py --once               # first pass over existing transcripts
python tools/search.py "what you remember"   # try it
```

Then **open a new Claude Code session**: hooks are read when a session starts, so the chat that
was already open does not have them. The first thing the new session prints is the briefing
(`## Memory: <agent>, <date>`). If it prints `(no index yet …)`, run the indexer once.

`install.py` touches only the `hooks` and `autoCompactWindow` keys of `~/.claude/settings.json`,
re-reads the file, and restores the backup on any error. The memory lives in
`~/.compact-and-recall` by default (`memory_root` in the config).

Name your agents with the `CR_AGENT` environment variable, or let the project directory name
stand for the agent. Standing decisions are read from `docs/decisions.md` in the project
(`decisions_file`), one decision per line starting with `D<n>`.

### Windows, VS Code and Claude Code, step by step

This is the setup it was built and tested on. Everything runs from the terminal inside VS Code
(Terminal > New Terminal); the hooks then apply to every Claude Code session of your user, in the
VS Code extension and in the `claude` command alike.

1. **Prerequisites.** Python 3.9 or later on the PATH (`python --version` in the VS Code terminal;
   the python.org installer with "Add python.exe to PATH" is enough, no package to install), Git,
   and Claude Code already signed in (the extension or the CLI).
2. **Clone to a stable place**, for example `%USERPROFILE%\tools\compact-and-recall`. The hooks
   point to the absolute path of this folder and to the Python that ran `install.py`: if you move
   the folder or change Python, run `python install.py` again (it replaces nothing, it adds the new
   commands; remove the old ones from `settings.json` by hand or restore the backup first).
3. **Check what will be used**: `python tools\cr_config.py`. Defaults: the memory under
   `%USERPROFILE%\.compact-and-recall`, the transcripts of every project of your user under
   `%USERPROFILE%\.claude\projects`. To change that, copy `config.example.json` to `config.json`
   next to `tools\` and edit `memory_root` or `transcripts_glob`. If you set the variable
   `COMPACT_AND_RECALL_CONFIG`, it must point to an existing file: a wrong path is refused, it never
   falls back to the defaults in silence.
4. **Install**: `python install.py --dry-run`, read the change, then `python install.py` and type
   `YES`. The file edited is `%USERPROFILE%\.claude\settings.json`; a dated backup sits next to it.
   Your `permissions`, `env`, `model` and other keys are left as they are, and this is asserted
   after the write.
5. **Index**: `python install.py --task` creates the scheduled task "compact-and-recall index"
   (every 10 minutes, with `pythonw.exe`, so no window); `python tools\indexer.py --once` runs the
   first pass now. Without an index the briefing and the recall print nothing.
6. **Open a new Claude Code session** (a new chat in VS Code, or `claude` in a new terminal). The
   briefing is printed at the top. Each later prompt that cites an exact anchor (a decision number
   such as `D12`, a build number such as `v1172`, a file name) gets a `[memory]` block of 3 to 5
   passages, only when those passages are outside the current context. No anchor, nothing found,
   or still in context: the hook prints nothing, by design. To see it work by hand:
   `echo {"session_id":"x","prompt":"what did we decide about D1","cwd":"."} | python tools\recall_hook.py`
   (with an index that contains `D1`; `examples\sample_transcript.jsonl` does).
7. **Name the agent.** By default the agent is the name of the folder opened in VS Code, which is
   right when one folder is one project. For a fixed name whatever the folder, set `CR_AGENT` in
   the environment Claude Code starts with (the `env` key of `settings.json` does it for every
   session).
8. **Give the project its decisions file and its rules.** Create `docs\decisions.md` in the
   project, one decision per line starting with `D<n>` (the briefing shows the last eight), and
   paste [docs/OPERATING_RULES.md](docs/OPERATING_RULES.md) into the project's `CLAUDE.md` so the
   agent resumes by the briefing, the episode and the search instead of re-reading everything.
9. **Uninstall**: copy the backup back over `settings.json` and run
   `schtasks /Delete /TN "compact-and-recall index" /F`. The memory folder is yours to delete.

## Repository

```
tools/        cr_config.py  secrets_filter.py  indexer.py  search.py  episodes.py
              briefing.py  recall_hook.py  graph.py  entity.py  check_secrets.py
              verify_gaps.py  test_secrets_filter.py
measures/     audit_sessions.py  measure_baseline.py  measure_resume.py  measure_speed.py
hooks/        settings.example.json
examples/     sample_transcript.jsonl  (synthetic, for the tests)
docs/         ARCHITECTURE.md  OPERATING_RULES.md  MEASUREMENTS.md  PRIOR_ART.md  SECURITY.md
install.py    config.example.json
```

## What is not here, on purpose

- **No vectors.** Our passages are commands, paths, identifiers and short human messages; exact
  match is what resumption needs, and claude-mem-lite measured a TF-IDF arm as net-negative. It
  is the first item of the roadmap; sqlite-vec would fit in the same file. Measure before adopting.
- **No LLM.** Episodes and the graph are built by patterns: crude, free, deterministic, private.
- **No service, no MCP server yet.** One file and three hooks.

## Where it comes from

We read [claude-mem-lite](https://github.com/sdsrss/claude-mem-lite),
[claude-mem](https://github.com/thedotmack/claude-mem), [memcp](https://github.com/maydali28/memcp),
[MemPalace](https://github.com/mempalace/mempalace) and the Anthropic hooks documentation before
building this, and borrowed from each. SQLite FTS5 with these three hooks is not new;
claude-mem-lite does it under the same license. What we add is the measurement on real sessions,
early compaction as a deliberate strategy, frozen episodes with provenance, a briefing made of the
human's exact words, and one shared database for many agents. The full list of what we borrowed,
what is not new, and twelve ideas we have not done yet is in
[docs/PRIOR_ART.md](docs/PRIOR_ART.md).

## Status

Alpha. Extracted on 29 October 2026 from a private fleet that has run it since 27 October.
Tested on Windows 11 with Python 3.12; the POSIX paths (`fcntl` lock, crontab) are written but
untested. The identifiers, table names and defaults were translated for publication; the
behaviour is the one in production. Issues and measurements from other setups are welcome,
especially a benchmark of resumptions.

## Privacy

Your transcripts contain whatever you typed. Read [docs/SECURITY.md](docs/SECURITY.md) before
installing: what the secrets filter catches, what it cannot, what is never indexed, what is never
rewritten. Nothing leaves the machine.

## License

MIT.

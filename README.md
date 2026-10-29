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
git clone https://github.com/<you>/compact-and-recall
cd compact-and-recall
cp config.example.json config.json          # optional; defaults work
python install.py --dry-run                  # shows the settings.json change, writes nothing
python install.py                            # dated backup, adds the hooks, sets autoCompactWindow
python install.py --task                     # Windows: scheduled task every 10 min; POSIX: prints a crontab line
python tools/indexer.py --once               # first pass over existing transcripts
python tools/search.py "what you remember"   # try it
```

`install.py` touches only the `hooks` and `autoCompactWindow` keys of `~/.claude/settings.json`,
re-reads the file, and restores the backup on any error. The memory lives in
`~/.compact-and-recall` by default (`memory_root` in the config).

Name your agents with the `CR_AGENT` environment variable, or let the project directory name
stand for the agent. Standing decisions are read from `docs/decisions.md` in the project
(`decisions_file`), one decision per line starting with `D<n>`.

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

# Architecture

## Why

A coding agent that runs for hours re-sends its whole live conversation to the model at every
call. On a fleet of 30-40 long-running agents on one machine we measured, over 90 minutes, that:

- 47 % of the spend was giant contexts re-read at every message (263k tokens for one agent,
  478k for another). The direct cause was a compaction window set at 700k tokens.
- 53 % was subagents launched by the dozen, each loading whole files (220k-270k tokens per call).
- The rest: every relayed message wakes an agent that re-reads its context; every resume of a big
  conversation rewrites it all into cache; a 1.2 MB memory file re-read at every start.

## The principle

You cannot make the harness send "only part" of the live conversation. The only lever is to keep
the live conversation small and to make everything else retrievable on demand. Nothing is lost;
it is stored elsewhere, and the agent goes to get what it needs.

```
 Live conversation (100k to 200k tokens at most)
   instructions + ~3k tokens pushed at start + the latest turns
         ^ at start and after each compaction          ^ at each prompt (<= 1k tokens)
         | standing decisions, latest episodes          | the 3 to 5 useful passages
 ------------------------------------------------------------------------------------
 External memory, one SQLite file under memory_root, no service, no model
   - INDEX: full text of every transcript, split by turn, secrets removed (FTS5, BM25)
   - EPISODES: frozen files, one per compaction and per end of work, never rewritten
   - GRAPH: decisions, questions, versions, scripts, files, agents, linked to passages
   refreshed every 10 minutes by one scheduled task; agents only read
```

## The six parts

| Part | What it does | Where |
|---|---|---|
| 1. Early compaction | `autoCompactWindow` at 200k (the model can usually take 1M). The same agent goes on in the same conversation; only the summary changes. | `install.py`, `hooks/settings.example.json` |
| 2. Frozen episodes | PreCompact hook: copy the raw messages since the last episode, write one file: human quotes verbatim, decisions and reasons, exact state, files touched, machine time. Never rewritten. Built without a model, by patterns. | `tools/episodes.py` |
| 3. Index of everything | SQLite FTS5 (`unicode61`, diacritics removed, BM25) of every transcript, split by turn, secrets filtered on write. Incremental by byte offset, single writer, subagent transcripts attributed to their parent session. | `tools/indexer.py`, `tools/secrets_filter.py` |
| 4. Briefing at start | SessionStart hook (startup, resume, compact): the last human messages verbatim, up to eight standing decisions from the project's decisions file, three latest episodes, the search command. Budget `briefing_chars`. | `tools/briefing.py` |
| 5. Recall at each prompt | UserPromptSubmit hook: exact anchors in the prompt (decision numbers, versions, file names) are looked up in the index; 3 to 5 passages, budget `recall_chars`, a cache avoids repeating a lookup, the current session before its last compaction is excluded. | `tools/recall_hook.py` |
| 6. Search on demand | `search.py` (FTS5 query, filters by agent, kind, date, one full turn), `entity.py` (everything about one entity), `graph.py` (update, stats, rebuild, ingest a hand-written map). | `tools/` |

Plus the measures that justify the whole thing: `measures/` reads the transcripts and the index
and prints the numbers in [MEASUREMENTS.md](MEASUREMENTS.md).

## Data model

```
files        (path, agent, project, session, is_subagent, parent_session, bytes_done, turn, mtime, last_indexed)
passages     (id, session, agent, project, subagent, turn, ts, kind, tool, files, uuid, text)
passages_fts (FTS5 over text; triggers keep it in sync)
episodes     (path, agent, at, source, bytes, passage)   one row per frozen episode file, indexed as a passage
removals     (kind, n)                     how many times each filter class fired, never the secret
entities     (id, type, name, scope, text)
mentions     (entity, passage)
links        (source, target, relation, passage, since)
graph_state  (key, value)
map_entities (map, entity)
```

`kind` is one of `human`, `instruction` (prompt given to a subagent), `agent`, `thinking`,
`tool` (command, an arrow, the truncated result), `relay` (automated message, only if
`relay_markers` is configured), `notification`, `compact_summary`, `command`, `episode`.

## What we deliberately do not do

- **No vectors.** Our passages are tool commands, file paths, identifiers and short human
  messages; exact match is what resumption needs. The authors of claude-mem-lite measured a TF-IDF
  arm as net-negative and removed it. We have not measured embeddings on our data yet; it is the
  first item of the roadmap in [PRIOR_ART.md](PRIOR_ART.md), and sqlite-vec would fit in the same
  file.
- **No LLM in the loop.** Episodes and the graph are built by patterns. It makes them crude, and
  it makes them free, deterministic and private.
- **No service.** One file, one scheduled task, three hooks. Nothing to keep running.
- **No `/clear` mid-work.** The person wanted to keep talking to the same agent.

## How the pieces talk

```
Claude Code  --SessionStart-->      briefing.py     registers (session, agent, cwd) in sessions.json
             --UserPromptSubmit-->  recall_hook.py  reads memory.db
             --PreCompact-->        episodes.py     writes episodes/<agent>/EPISODE_<date>_<time>_<source>.md
scheduler (every 10 min) -->        indexer.py      reads transcripts, writes memory.db, then graph.py update
you -->                             search.py, entity.py, measures/*.py   read-only on memory.db
```

Agent attribution: `sessions.json` (written at SessionStart from `$CR_AGENT` or the project
directory name) first, then the project folder encoded in the transcript path. Subagent
transcripts take their parent's agent.

# Operating rules for an agent that runs with this memory

The tools alone do not save tokens. The agent has to change how it resumes after a compaction.
These rules are the ones we gave to every agent of the fleet this was built for. They are written
as instructions you can paste into a CLAUDE.md or a system prompt. "You" is the agent.

## 1. What the memory is (read-only for you)

- **The index of everything**: every human message, agent message, tool result and compaction
  summary of every session, secrets removed, in one SQLite file, refreshed every 10 minutes by one
  scheduled indexer. You never write to it.
- **Frozen episodes**: at each compaction and at each end of a piece of work, one file that is
  never rewritten: the human's exact words, the exact state of the work, files touched, machine
  time.
- **A light graph**: decisions, questions, versions, scripts and files, linked to the passages that
  mention them. No model builds it; regexes do.

## 2. What is pushed to you without asking

At startup, on resume and after each compaction, a short briefing arrives: the last five human
messages verbatim, the standing decisions of your project, your three latest episodes, and the
search command. The briefing wins over the compaction summary, which paraphrases.

## 3. The order of resumption after a compaction, and nothing else

1. The pushed briefing.
2. Your latest pre-compaction episode.
3. Your state file, then the last five entries of your journal. Never the whole journal.
4. For anything older: search (rule 4).

Forbidden: re-reading a long onboarding document, a handover, a journal, a memory index or any big
file in full after a compaction. We measured it: when agents follow the order above, zero
resumptions out of 226 re-read a long document.

## 4. Search before re-reading, search before asking

- `python tools/search.py <words> --agent <YOU>` with `--kind human` (the person's exact words),
  `--since 2026-10-20`, `--recent`, `--turn <session> <n>` (one full turn). A path, an
  identifier, a decision number, a file name are searched as they are.
- `python tools/entity.py <D71 | script.py | ...>`: everything the graph knows about one thing.
- Before any question to the human: search their words on the subject, then read the decisions
  file. A question that was already settled is never asked again.
- End of a piece of work: `python tools/episodes.py end --agent <YOU>`, then tell the human.

## 5. Compacting at 200k is not a loss

The thread of your work lives on disk, not in your context window. Before each heavy step, write
where you are (state file, journal: done, in progress, next). When the compaction comes, it costs
you nothing: you resume by rule 3. Do not keep large tool outputs in context, they are in the
index. A session is reopened at the end of a piece of work, not in the middle.

## 6. Subagents are bounded, or there is no subagent

Measured on one night: the subagents of a single agent made 30 % of the machine's spend because
eleven of them compacted up to three times each.

1. A subagent gets a finished piece (one chapter, one file to review, one list to check), a
   20-line brief, and a named output file.
2. It must never reach a compaction: if you expect more than 40 tool calls or 120k tokens, split
   before launching. A subagent that compacted is a failed split; note it in the journal.
3. At most three at a time, never the same batch twice, never in bursts without the human's go.
4. You read the file it produced, not its transcript. Its final report: file produced, what is
   missing, what blocked.
5. The cheapest model for mechanical work; the strong model only to write or to decide.

## 7. Messages and secrets

- Messages between agents are batched: one report per hour, not one per batch. Each message
  received costs the recipient a re-read of its whole context.
- A secret or a defect seen in a search result: report the passage id, never the content.

## 8. The proof

At your next resumption after a compaction, five lines in your journal: which episode you read,
which search you ran, which state file put you back on the thread, and what you recovered that you
would have lost otherwise. An agent with nothing to write there is not using the memory.

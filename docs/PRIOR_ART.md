# Prior art, what we borrowed, and what we have not done yet

We did not invent most of this. Before building, we read the projects below; after building, we
went back to see what they do that we do not. Star counts are rounded snapshots from late
October 2026.

## Projects we learned from

| Project | What we took from it |
|---|---|
| [claude-mem-lite](https://github.com/sdsrss/claude-mem-lite) (MIT, ~70 stars) | The closest cousin: SQLite FTS5 only, PreCompact and SessionStart hooks, recall at each prompt, a secrets filter, a token budget at startup. They measured a TF-IDF arm as net-negative and removed it, which is why we start without vectors. We do not call a small model to encode episodes; they do. |
| [claude-mem](https://github.com/thedotmack/claude-mem) (Apache-2.0, ~104k stars) | Recall at `UserPromptSubmit`, progressive disclosure (search returns ids and titles, the full text comes on demand), the `<private>` tag idea. We do not run an LLM per tool call. |
| [memcp](https://github.com/maydali28/memcp) (MIT) | A blocking PreCompact hook, typed graph edges with reinforcement and decay. Our graph is much simpler. |
| [MemPalace](https://github.com/mempalace/mempalace) (MIT, ~60k stars) | Verbatim storage, validity windows on graph facts, retro-indexing of existing transcripts, benchmarks published without an LLM judge. |
| [Anthropic hooks documentation](https://code.claude.com/docs/en/hooks) and [memory tool documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) | The hook events and their stdin JSON; the advice to expire memory files that are never read and to filter sensitive data before storing. |
| [mem0](https://github.com/mem0ai/mem0), [Letta](https://github.com/letta-ai/letta), [Graphiti](https://github.com/getzep/graphiti), [cognee](https://github.com/topoteretes/cognee) | What we chose not to do: a server, a vector store, an LLM extracting facts. Good at scale, too heavy for one machine with no service and no API key allowed. |

## What is not new here

- SQLite + FTS5 and the three hooks: claude-mem-lite, claude-mem, memcp do it.
- A secrets filter before indexing: claude-mem-lite (patterns), claude-mem (`<private>`).
- Recall of 3 to 5 passages at each prompt: claude-mem, MemPalace.
- An entity graph in SQLite: memcp, MemPalace.
- Retro-indexing of transcripts: MemPalace `mine`.

## What we think is new, or at least rare

- **Production measurements on a fleet**, read from the transcripts: cost per call, context per
  call, compactions, subagent share, before and after, with the method published
  ([MEASUREMENTS.md](MEASUREMENTS.md)). The projects above give architectural estimates
  ("600x cheaper"), modelled baselines, or benchmark scores from a managed service. None we found
  publishes a before/after on real sessions.
- **Early compaction as a deliberate strategy** (200k window when the model takes 1M), with the
  memory compensating. Others suffer compaction and try to survive it; none triggers it on
  purpose and measures the trade-off.
- **Frozen episodes**, one per compaction, never rewritten, with provenance. Others store
  observations that get updated, summarised or decayed.
- **A briefing made of the human's exact words** (last five messages verbatim) plus the standing
  decisions, rather than a generated summary. It exists because of one rule: never ask again a
  question the person already settled.
- **One shared database for many agents** with a light graph and no extraction model.

## Roadmap: ideas we found and have not done

Ordered by how much we expect from them. Each one should be measured on our own resumptions
before it is kept.

1. **Local vectors in the same SQLite file** (sqlite-vec with a tiny local model such as
   Model2Vec or MiniLM), fused with BM25 by reciprocal rank fusion (k=60). Seen in
   [vstash](https://arxiv.org/pdf/2604.15484), QMD, MemPalace `sqlite_exact`. Guard: claude-mem-lite
   measured TF-IDF as net-negative; measure first.
2. **Decay by type with half-lives** (decisions 90 days, bug fixes 14, changes 7) and compression
   of old observations into summaries (claude-mem-lite); MMR re-ranking for diversity
   (starpod-memory).
3. **Two-stage deduplication**: Jaccard within a 5-minute window, MinHash over 7 days across
   sessions (claude-mem-lite). On 582k passages from 30-40 agents, duplicates probably weigh a lot.
4. **Progressive disclosure in three steps**: `search` (ids and 50-100 tokens per hit),
   `timeline` (context around a passage), `get` (full text) (claude-mem). Our recall hook could
   inject titles and ids only.
5. **Blocking PreCompact** until the episode is written and verified, and a PostCompact check that
   the briefing was re-injected (memcp; both hooks exist in the official documentation).
6. **Retro-indexing and an idempotent, resumable sweep** (MemPalace), useful for a new machine
   and to prove nothing was lost after a crash. `verify_gaps.py` is a first step.
7. **A reusable benchmark**: LongMemEval-S or LoCoMo for comparison, and 100-200 questions drawn
   from our own episodes with a target passage, scored by recall@k and MRR without an LLM judge
   (an audit of the LoCoMo judge found it accepts up to 63 % of wrong answers).
8. **Cost measured from transcripts, with the method**, extended: cache hits and misses per call,
   compactions per 100k tokens, cost per episode. Nobody publishes this; we started.
9. **Typed graph edges reinforced by co-recall** and decayed over time (memcp).
10. **Validity windows on graph facts** (`valid_from`, `valid_until`) so a revoked decision is
    excluded from recall instead of deleted (MemPalace, Graphiti). Fits our never-rewrite rule.
11. **A `<private>` tag** the human or the agent can put around a passage that must never be
    indexed (claude-mem), on top of the regex filter.
12. **Expiration of episodes never read again**: log the last access, archive after N days
    (Anthropic memory tool guidance).

## Community signals that the problem is real

- [anthropics/claude-code#43733](https://github.com/anthropics/claude-code/issues/43733): state
  persistence across compactions.
- "59 compactions, built our own" and a proposal to expose compaction lifecycle hooks for external
  memory layers, both on the Claude Code issue tracker.

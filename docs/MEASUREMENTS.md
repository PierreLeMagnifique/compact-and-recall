# Measurements

Everything here was measured on one fleet of long-running Claude Code agents (30 to 40 sessions
per day, one Windows machine, Opus and Fable models, one person giving instructions by voice).
The scripts are in `measures/`. The numbers below are ours; run the scripts on your own
transcripts before believing any of it applies to you.

## Method

All token figures are **weighted input-equivalent tokens** read from the `usage` block of every
assistant message in the transcripts (`~/.claude/projects/**/*.jsonl`):

```
weight = input
       + 1.25 x cache_creation (5 min)
       + 2.00 x cache_creation (1 h)
       + 0.10 x cache_read
       + 5.00 x output
```

The weights follow the published price ratios of the models we use; `audit_sessions.py` lets you
change them. "Context carried per call" is input + cache creation + cache read of one call.
A compaction is a `compact_boundary` system line; its size is the `preTokens` it reports.
Subagent transcripts (`<session>/subagents/*.jsonl`) are counted with their parent.

Windows: **before** = 20 to 27 October 2026, 17:30 (compaction window 700k, no memory);
**after** = 27 October 17:30 to 29 October 08:20 (window 200k, hooks and index installed, agents
given the operating rules on the 28th). Resumption and speed figures come from the SQLite index
(582k passages) with `measure_resume.py` and `measure_speed.py`; cost figures from four audits of
the transcripts with `audit_sessions.py`.

## Results

| Indicator | Before | After | Reading |
|---|---|---|---|
| Cost of one call (weighted, subagents excluded) | 52.9k | 24.5k / 23.3k / 21.9k (three consecutive audits) | divided by 2.3 |
| Context carried per call (mean) | 381k | 127k / 118k / 115k | divided by 3 |
| Size of a compaction | 667k (5 in the witness night) | 167k-169k (188 compactions over three audits) | the 200k window is applied everywhere |
| Share of subagents in the spend | 53 % (70.8M of 134.8M) | 34 %, then 5 %, then 12 % | bounding rule applied, to be watched |
| Spend of one night, similar perimeter | 134.8M (8 sessions) | 76.9M (10 sessions), then 39.4M (16 sessions) | less money: yes |
| Resumptions after compaction that query the memory (next 15 calls) | 0 of 143 (0 %) | 25 of 228 (11 %) | the memory is used, by one resumption in nine |
| Memory tool calls (search, episodes, entity) per 1,000 calls | 0 | 36.2 (506 calls) | from nothing to routine |
| Resumptions that re-read a long document in full | 0 of 141 | 0 of 226 | the rule is kept |
| Median gap between two tool calls (speed of one step) | 4.7 s | 5.2 s | not faster: a step depends on the model and the tool, not on the context size |
| Human messages per 1,000 calls (how often the person had to step in) | 17.9 | 18.9 | unchanged |

Per agent, use of the memory at resumption (after) ranges from 67 % down to 0 %. The two heaviest
consumers, 110 of the 228 compactions, never queried it: they resume from the summary and the
pushed briefing alone. Their first productive action comes 0.1 to 0.2 minute after the compaction,
before and after. That 0 % does not prove they lost the thread; only a judged benchmark would.

## What we could not measure

- **"Smarter"**: no direct measure. Usable proxies: questions asked again on a settled point,
  errors repeated across compactions, work redone. It needs a judge reading random resumptions
  before and after with a binary grid. We have not run it.
- **"Faster" as in a job finished sooner**: no identical job on both sides. What we can say: a
  call costs 2.3 times less, so at equal budget the agent takes 2.3 times more steps.
- **What the compaction summary itself loses**: only a judge measures that.

## Lessons from measuring

- Count the tool **command**, not the tool result, when you grep transcripts for "did the agent
  re-read X". A directory listing that names a file, or a grep output that cites it, is not a
  re-read. Our first numbers said 8 % and 11 % of resumptions re-read a long document; counting
  the command only, both are 0 %. The same mistake inflated "resumptions that query the memory"
  from 11 % to 33 %.
- Journals have several names. A search for proof lines that looks at one file name
  under-counts.
- Weighted tokens are a proxy. The real invoice is the number that matters, and it moved the same
  way.

## Reproduce

```
python measures/audit_sessions.py --since 2026-10-26T22:00:00Z --until 2026-10-27T08:00:00Z
python measures/measure_baseline.py <session-id>
python measures/measure_resume.py --before 2026-10-20 2026-10-27T17:30 --after 2026-10-27T17:30 2026-10-29T08:20
python measures/measure_speed.py  --before 2026-10-20 2026-10-27T17:30 --after 2026-10-27T17:30 2026-10-29T08:20
```

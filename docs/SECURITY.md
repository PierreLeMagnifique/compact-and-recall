# Security and privacy

compact-and-recall indexes your own conversations with a coding agent. That text can contain
anything you typed or pasted: keys, passwords, customer data, prices, names. This page says what
the tools do about it and what they cannot do.

## Threat model

- The index is a plain SQLite file under `memory_root`. Anyone who can read that file can read
  every indexed passage. Protect it like you protect the transcripts themselves
  (`~/.claude/projects`), which hold the same text unfiltered.
- Nothing leaves the machine. There is no network call, no service, no telemetry, no LLM.
- The hooks print text that is injected into the model's context. That text comes from your own
  past sessions, so it is as trustworthy as your past sessions were. A prompt injection you pasted
  last month can come back through recall. Treat recalled passages as data, not instructions.
- The scheduled indexer runs with your user rights. It only writes under `memory_root` and never
  modifies a transcript.

## What is never indexed

- Secrets caught by `tools/secrets_filter.py` on write: API keys with known prefixes, JWTs,
  `Bearer` tokens, basic-auth URLs, `key=value` assignments of secret-looking names, long hex
  strings, base64 runs, PEM blocks, e-mail addresses, and password phrases in several languages.
  The filter keeps the count per class in the `removals` table; the secret itself is never stored.
- Values of the `sensitive_fields` you configure (defaults: purchase prices and margins), both as
  JSON fields and in prose.
- Anything matching your private regexes in `secret_patterns.txt` (kept outside the repo, listed
  in `.gitignore`).
- Passages matching `exclude_patterns` (for example blind benchmark material).

The filter is a regex filter. It has no understanding of meaning. It will miss a secret that
looks like an ordinary word and may remove an innocent long hex string. Run
`python tools/test_secrets_filter.py` after changing patterns and `python tools/check_secrets.py`
on a schedule: it reports passage ids and classes, never the text.

## What is never rewritten

- Episodes are written once. The hook refuses to overwrite an existing episode file.
- The indexer only appends passages and advances byte offsets. It never deletes a passage. If you
  need to purge something, delete the rows yourself and keep the id in mind (`check_secrets.py`
  gives it to you).

## Installing hooks

`install.py` edits `~/.claude/settings.json`. It makes a dated backup first, touches only the
`hooks` and `autoCompactWindow` keys, re-reads the file, and restores the backup if anything is
off. Run it with `--dry-run` first and read the diff. It never touches `permissions`.

## Before you publish anything built on this

Run the leak scan you need for your own context (names, paths, domain words) over the repo and
over the SQLite file. The repo you are reading was scanned for the private names, paths and
vocabulary of the fleet it comes from before publication; the data (index, episodes, transcripts,
measures per named agent) was never copied into it.

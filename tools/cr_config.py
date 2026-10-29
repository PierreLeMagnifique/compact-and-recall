"""Shared configuration for compact-and-recall.

Resolution order for the config file:
  1. $COMPACT_AND_RECALL_CONFIG
  2. <repo>/config.json (next to this tools/ folder)
  3. ~/.compact-and-recall/config.json
Missing keys fall back to DEFAULTS, so an empty config.json is valid.
Everything is local: no network, no service, one SQLite file under memory_root.
"""
import copy
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULTS = {
    # Where the SQLite index, the episodes and the session registry live.
    "memory_root": "~/.compact-and-recall",
    # Claude Code transcripts (main sessions and their subagents/*.jsonl).
    "transcripts_glob": "~/.claude/projects/**/*.jsonl",
    # Label used for the person's messages in the index and the briefings.
    "human_label": "human",
    # How a session is attributed to an agent (a named long-running role, a project, a person).
    # env_var: read at SessionStart from the environment; fallback: "project_dir" (basename of cwd) or "unknown".
    "agent": {"env_var": "CR_AGENT", "fallback": "project_dir"},
    # Optional regex applied to the first tool commands of a session to recover the agent name
    # (for example "--agent (\\w+)"). null disables it.
    "agent_declaration_regex": None,
    # Path, relative to the project directory, of the markdown file that holds the standing decisions.
    "decisions_file": "docs/decisions.md",
    # Optional markers at the start of a human message that identify an automated relay rather than the person.
    "relay_markers": [],
    # Passages matching one of these regexes are never indexed (for example blind benchmark material).
    "exclude_patterns": [],
    "episodes": {"dir": "episodes", "max_chars": 12000},
    "budgets": {"briefing_chars": 6000, "recall_chars": 3500, "recall_hits": 5},
    # Exact anchors that trigger the per-prompt recall hook (short tokens that name a thing precisely).
    "anchor_patterns": [
        r"(?<![\w/\\:])D\d{1,3}\b",                      # decision numbers   D12
        r"(?<![\w/\\])Q\d{1,3}[a-z]?\b",                 # open questions     Q4
        r"(?<![\w/.])v\d{3,4}\b",                        # build numbers      v1172
        r"\b[\w-]{3,}\.(?:py|sh|ps1|md|json|ts|js)\b",   # file names
    ],
    # Entity types for the light graph: name -> regex. Decisions and questions are scoped per agent.
    "entity_patterns": {
        "decision": r"(?<![\w/\\:])D\d{1,3}\b",
        "question": r"(?<![\w/\\])Q\d{1,3}[a-z]?\b",
        "version": r"(?<![\w/.])v\d{3,4}\b",
        "script": r"\b[\w-]{3,}\.(?:py|sh|ps1)\b",
    },
    "entity_scoped_types": ["decision", "question"],
    # Field names whose values are business-sensitive in your context (removed by the secrets filter).
    "sensitive_fields": ["purchase_price", "cost_price", "margin", "wholesale_price"],
    # Extra regexes (one per line) for the secrets filter, kept outside the repo.
    "extra_secret_patterns_file": "secret_patterns.txt",
    "index": {"max_input_chars": 2000, "max_output_head": 1500, "max_output_tail": 500,
              "interval_minutes": 10, "batch_files": 400},
    "sessions_file": "sessions.json",
}


def _deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_path():
    for p in (os.environ.get("COMPACT_AND_RECALL_CONFIG"), os.path.join(REPO, "config.json"),
              os.path.expanduser("~/.compact-and-recall/config.json")):
        if p and os.path.exists(p):
            return p
    return None


_CACHE = None


def load():
    """The merged configuration, with memory_root and transcripts_glob expanded to absolute paths."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    extra = {}
    p = config_path()
    if p:
        with open(p, encoding="utf-8") as f:
            extra = json.load(f)
    cfg = _deep_merge(DEFAULTS, extra)
    cfg["memory_root"] = os.path.abspath(os.path.expanduser(os.path.expandvars(cfg["memory_root"])))
    cfg["transcripts_glob"] = os.path.expanduser(os.path.expandvars(cfg["transcripts_glob"]))
    cfg["_config_file"] = p
    _CACHE = cfg
    return cfg


def memory_root():
    root = load()["memory_root"]
    os.makedirs(root, exist_ok=True)
    return root


def db_path():
    return os.path.join(memory_root(), "memory.db")


def episodes_dir(agent=None):
    d = os.path.join(memory_root(), load()["episodes"]["dir"])
    return os.path.join(d, agent) if agent else d


def sessions_path():
    return os.path.join(memory_root(), load()["sessions_file"])


def _read_sessions():
    p = sessions_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def register_session(session_id, agent, cwd, extra=None):
    """Called by the SessionStart hook. Adds or updates one entry; never removes anything."""
    if not session_id:
        return
    sessions = _read_sessions()
    entry = sessions.get(session_id, {})
    entry.update({"agent": agent, "cwd": cwd})
    if extra:
        entry.update(extra)
    sessions[session_id] = entry
    tmp = sessions_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=1)
    os.replace(tmp, sessions_path())


def session_info(session_id):
    return _read_sessions().get(session_id or "", {})


def project_of_transcript(transcript_path):
    """Claude Code stores transcripts under ~/.claude/projects/<cwd with separators replaced by '-'>/."""
    parts = os.path.normpath(transcript_path).split(os.sep)
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


def agent_of(session_id, transcript_path=None):
    """Agent name for a session: registry first, then the project folder encoded in the transcript path."""
    info = session_info(session_id)
    if info.get("agent"):
        return info["agent"]
    if transcript_path:
        return project_of_transcript(transcript_path)
    return "unknown"


def current_agent(cwd=None):
    """Agent name at SessionStart: environment variable, else the fallback rule."""
    cfg = load()["agent"]
    name = os.environ.get(cfg.get("env_var") or "", "").strip()
    if name:
        return name
    if cfg.get("fallback") == "project_dir":
        return os.path.basename(os.path.normpath(cwd or os.getcwd())) or "unknown"
    return "unknown"


def compiled(patterns):
    return [re.compile(p) for p in patterns]


def utf8_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    utf8_stdout()
    c = load()
    print("config file:", c["_config_file"] or "(defaults only)")
    print("memory_root:", c["memory_root"])
    print("transcripts:", c["transcripts_glob"])
    print("db         :", db_path())

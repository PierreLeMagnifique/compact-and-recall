"""Secrets filter for compact-and-recall.

Applied to every text before it enters the index: human and agent messages,
tool calls, tool outputs, compaction summaries and episodes.

Two families of patterns:
  1. Credentials and personal identifiers: PEM private keys, JWT, API keys with
     a known prefix, platform tokens, bearer headers, credentials inside URLs,
     ``key = value`` assignments whose key names a secret (password words in
     several languages), short variables receiving a long random string,
     long hexadecimal runs, base64 runs recognised by their character-class
     jumps, e-mail addresses.
  2. Business-sensitive material, configured in config.json:
     ``sensitive_fields`` (field names; neutral defaults such as
     ``purchase_price``) and ``sensitive_prose_patterns`` (phrases that must
     not be followed by an amount; neutral defaults).
  Extra private regexes can live outside the repo, one per line, in the file
  named by ``extra_secret_patterns_file`` (resolved under memory_root when the
  name is relative). A line that is not a valid regex is matched literally.

Usage from code::

    from secrets_filter import clean, residues
    text = clean(text, counts)   # counts: collections.Counter, one key per class
    assert not residues(text)    # a cleaned text has no residue

This module never returns or prints the removed text, only counts per class.
It never reads or writes the index and never touches the network.
"""
import os
import re
from collections import Counter

import cr_config

_KEY_WORDS = (r'api[_-]?key|apikey|secret|token|password|passwd|pwd|passphrase|mot[ _]de[ _]passe|mdp|'
              r'contrase[\u00f1n]a|passwort|wachtwoord|senha|access[_-]?key|private[_-]?key|client[_-]?secret|'
              r'auth[_-]?key|credential')

_DEFAULT_PROSE = [r'purchase\s+price', r'cost\s+price', r'wholesale\s+price', r'(?:gross|net|profit)\s+margins?']

# Placeholder for a field name alone, and for a field with its value.
FIELD_PLACEHOLDER = '[SENSITIVE_FIELD]'


def _field_regex(names):
    """One regex for all configured field names.

    ``purchase_price`` also matches ``purchasePrice`` and ``purchase_price_excl``; a single word such as
    ``margin`` only matches with a suffix (``margin_rate``, ``marginRate``) so that CSS ``margin: 0`` stays.
    The name is removed even when glued to another word (``step_purchase_price_update``), hence the
    look-behind instead of ``\\b``. An optional ``: value`` or ``= value`` goes with it.
    """
    alts = []
    for name in names or []:
        parts = [re.escape(p) for p in str(name).lower().split('_') if p]
        if not parts:
            continue
        if len(parts) == 1:
            alts.append(parts[0] + r'(?:_\w+|(?-i:[A-Z])\w*)')
        else:
            alts.append(parts[0] + ''.join('_?' + p for p in parts[1:]) + r'\w*')
    if not alts:
        return None
    return re.compile(
        r'(?i)["\']?(?<![A-Za-z0-9])(?:' + '|'.join(alts) + r')\b["\']?'
        r'(?:\s*[:=]\s*(?:"[^"\n]{0,80}"|\'[^\'\n]{0,80}\'|[\d.,]+\s*(?:%|\u20ac|EUR|USD|\$)?|null|None))?')


def _prose_regex(patterns):
    pats = [p for p in (patterns or []) if p]
    if not pats:
        return None
    return re.compile(r'(?i)\b(?:' + '|'.join('(?:%s)' % p for p in pats) + r')'
                      r'[^.\n]{0,40}?\d+(?:[.,]\d+)?\s*(?:%|\u20ac|EUR|USD|\$)')


def _build_patterns():
    cfg = cr_config.load()
    pats = [
        ('private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)')),
        ('jwt', re.compile(r'\beyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}')),
        ('sk_key', re.compile(r'\bsk-[A-Za-z0-9_\-]{16,}')),
        ('platform_token', re.compile(r'FlyV1\s+[^\s"\'\\`]+|\bf[mo][12]_[A-Za-z0-9_+/=\-]{16,}')),
        ('forge_token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|glpat-[\w-]{20,}|xox[abpr]-[\w-]{10,})')),
        ('cloud_key', re.compile(r'\b(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,})')),
        ('bearer', re.compile(r'\b(?:Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=\-]{12,}')),
        ('url_credentials', re.compile(r'(?<=://)[^\s/:@"\']+:[^\s/@"\']+(?=@)')),
        ('key_value', re.compile(r'(?i)\b([\w.-]*(?:' + _KEY_WORDS + r')[\w.-]*)(["\']?\s*[:=]\s*["\']?)([^\s"\',;}{)\]]{6,})')),
        # K='<long string>': a short or upper-case variable receiving a long string made of no words.
        ('assignment', re.compile(r'(?<![\w$])((?:[A-Za-z]|[A-Z][A-Z0-9_]{1,40})\s*=\s*)(["\'])([A-Za-z0-9+/=_\-.:]{32,})\2')),
        ('long_hex', re.compile(r'\b[0-9a-fA-F]{32,}\b')),
        ('email', re.compile(r'\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b')),
    ]
    rx = _field_regex(cfg.get('sensitive_fields'))
    if rx is not None:
        pats.append(('sensitive_field', rx))
    rx = _prose_regex(cfg.get('sensitive_prose_patterns', _DEFAULT_PROSE))
    if rx is not None:
        pats.append(('sensitive_prose', rx))
    return pats


PATTERNS = _build_patterns()

_B64 = re.compile(r'[A-Za-z0-9+/_\-]{32,}={0,2}')

# Addresses that are not personal and may stay (commit attribution of the tool itself).
KEPT_EMAILS = tuple(e.lower() for e in cr_config.load().get('kept_emails', ['noreply@anthropic.com']))


def _jumps(s):
    def cls(c):
        return 'd' if c.isdigit() else 'M' if c.isupper() else 'm' if c.islower() else 'x'
    return sum(cls(a) != cls(b) for a, b in zip(s, s[1:]))


def _readable(p, minimum=4):
    """A segment made of letters with few case changes: a word, a folder name, an upper-case tag."""
    return len(p) >= minimum and p.replace('_', '').replace('-', '').isalpha() and _jumps(p) <= len(p) / 3


def _b64_suspect(s):
    if len(s) < 32:
        return False
    if '-' in s and all(len(p) <= 12 for p in s.split('-')):
        return False  # dashed identifier (uuid, encoded project folder)
    words = sum(len(p) for p in re.split(r'[/_\-+.]', s) if len(p) >= 3 and _readable(p, 3))
    if words >= 0.3 * len(s):
        return False  # file name or identifier made of words
    digits = sum(c.isdigit() for c in s)
    upper = sum(c.isupper() for c in s)
    lower = sum(c.islower() for c in s)
    # A random key mixes the three classes and changes class almost at every character (about 0.65 of the
    # time); a code identifier (CamelCase, snake_case with a date) changes rarely (about 0.35).
    jumps = _jumps(s)
    return upper >= 4 and lower >= 4 and (
        (digits >= max(4, len(s) // 12) and jumps >= 0.45 * len(s)) or (digits >= 1 and jumps >= 0.5 * len(s)))


def _b64_pieces(s):
    """A path (at least two '/' and two readable segments) is judged segment by segment; a base64 key may
    contain '/' without forming words."""
    pieces = s.split('/')
    if len(pieces) >= 3 and sum(_readable(p) for p in pieces) >= 2:
        return pieces
    return [s]


def _extra_patterns():
    cfg = cr_config.load()
    name = cfg.get('extra_secret_patterns_file')
    if not name:
        return None
    name = os.path.expanduser(name)
    path = name if os.path.isabs(name) else os.path.join(cfg['memory_root'], name)
    try:
        with open(path, encoding='utf-8') as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    except OSError:
        return None
    parts = []
    for line in lines:
        try:
            re.compile(line)
            parts.append('(?:%s)' % line)
        except re.error:
            parts.append(re.escape(line))
    return re.compile('|'.join(parts), re.I) if parts else None


EXTRA = _extra_patterns()


def _assignment_is_secret(v):
    """The value of K='...' is a key when it is not made of words (a path, an address, a file name are)."""
    words = sum(len(p) for p in re.split(r'[/_\-+.:]', v) if _readable(p, 3))
    return words < 0.25 * len(v) and sum(c.isdigit() for c in v) >= 2 and sum(c.isalpha() for c in v) >= 8


def clean(text, counts=None):
    """Return the text without secrets. ``counts`` (a Counter) receives the number of removals per class.
    One pass can expose a new match (two pieces glued together), so we iterate while residues remain."""
    counts = counts if counts is not None else Counter()
    for _ in range(4):
        text = _one_pass(text, counts)
        if not text or not residues(text):
            break
    return text


def _one_pass(text, counts):
    if not text:
        return text
    for name, rx in PATTERNS:
        if name == 'key_value':
            def rep(m):
                if (m.group(3).isdigit() and len(m.group(3)) < 12) or m.group(3).startswith('['):
                    return m.group(0)  # max_tokens: 4096, or a value already removed
                counts[name] += 1
                return m.group(1) + m.group(2) + '[SECRET]'
            text = rx.sub(rep, text)
        elif name == 'email':
            def rep(m):
                if m.group(0).lower() in KEPT_EMAILS:
                    return m.group(0)
                counts[name] += 1
                return '[EMAIL@' + m.group(1) + ']'
            text = rx.sub(rep, text)
        elif name == 'assignment':
            def rep(m):
                if not _assignment_is_secret(m.group(3)):
                    return m.group(0)
                counts[name] += 1
                return m.group(1) + m.group(2) + '[SECRET]' + m.group(2)
            text = rx.sub(rep, text)
        else:
            text, n = rx.subn('[' + name.upper() + ']', text)
            if n:
                counts[name] += n

    def rep_b64(m):
        pieces = _b64_pieces(m.group(0))
        if not any(_b64_suspect(p) for p in pieces):
            return m.group(0)
        counts['b64'] += 1
        if len(pieces) == 1:
            return '[B64]'
        return '/'.join('[B64]' if _b64_suspect(p) else p for p in pieces)
    text = _B64.sub(rep_b64, text)
    if EXTRA is not None:
        text, n = EXTRA.subn('[REDACTED]', text)
        if n:
            counts['extra'] += n
    return text


def residues(text):
    """Counts of the patterns still present (used to audit the index): a cleaned text gives an empty Counter."""
    c = Counter()
    for name, rx in PATTERNS:
        for m in rx.finditer(text):
            if name == 'key_value' and (m.group(3).startswith('[') or (m.group(3).isdigit() and len(m.group(3)) < 12)):
                continue
            if name == 'email' and (m.group(0).lower() in KEPT_EMAILS or m.group(0).startswith('EMAIL@')):
                continue
            if name == 'assignment' and not _assignment_is_secret(m.group(3)):
                continue
            c[name] += 1
    c['b64'] += sum(1 for m in _B64.finditer(text) if any(_b64_suspect(p) for p in _b64_pieces(m.group(0))))
    if EXTRA is not None:
        c['extra'] += len(EXTRA.findall(text))
    return +c

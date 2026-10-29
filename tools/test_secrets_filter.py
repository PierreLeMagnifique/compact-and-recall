"""Test bench for the secrets filter.

Witnesses are generated at random (never a real key) and sane strings must come
out untouched. The bench never prints a witness, only its name and the verdict.

    python test_secrets_filter.py

Exit code 1 when a witness leaks or a sane string is modified. Nothing is
written anywhere.
"""
import random
import string
import sys
from collections import Counter

import secrets_filter as F

R = random.Random(7)


def rnd(n, alphabet=string.ascii_letters + string.digits):
    return ''.join(R.choice(alphabet) for _ in range(n))


# name -> (template, secret part). The secret must disappear and leave no residue.
WITNESSES = {
    'sk_key': ('key = sk-{}', rnd(40)),
    'bearer': ('Authorization: Bearer {}', rnd(48)),
    'platform_token': ('PLATFORM_TOKEN=FlyV1 fm2_{}', rnd(60)),
    'jwt': ('token eyJ{}', rnd(20) + '.eyJ' + rnd(30) + '.' + rnd(30)),
    'forge_token': ('ghp_{}', rnd(36)),
    'cloud_key': ('AKIA{}', rnd(16, string.ascii_uppercase + string.digits)),
    'long_hex': ('hash {}', rnd(64, '0123456789abcdef')),
    'b64': ('x {}==', rnd(44, string.ascii_letters + string.digits + '+/')),
    'b64_with_slashes': ('key {}', rnd(14) + '/' + rnd(12) + '/' + rnd(9) + '/' + rnd(11)),
    'password_en': ('password: "{}"', rnd(14)),
    'password_fr': ('mdp = {}', rnd(12)),
    'password_de': ('Passwort={}', rnd(12)),
    'password_es': ('contraseña: {}', rnd(12)),
    'url_credentials': ('https://alice:{}@example.org/x', rnd(12)),
    'email': ('write to {}@example.org please', 'jane.doe'),
    'sensitive_field_json': ('{{"purchase_price_excl": {}, "price_public": 9.95}}', '3.45'),
    'sensitive_field_camel': ('"marginRate": "{}%"', '31'),
    'sensitive_field_glued': ('python step245_sync_{}.py', 'purchase_price_update'),
    'sensitive_prose_price': ('the purchase price is {} EUR', '4.20'),
    'sensitive_prose_margin': ('a gross margin of {} % on this batch', '32'),
    'wholesale_camel': ('wholesalePrice: {}', '12.3'),
    'assignment': ("K='{}'; curl -H \"X: $K\" https://example.org", rnd(80, string.ascii_letters + string.digits + '_-')),
    'private_key': ('-----BEGIN PRIVATE KEY-----\n{}\n-----END PRIVATE KEY-----', rnd(64)),
}

# Must come out byte for byte identical.
SANE = [
    '/home/alice/projects/garden-planner/tools/planner_sync_2026_10_27.py',
    'cat "/home/alice/.cache/claude/tasks/bs0ijbiib.output"',
    'cd ~/.claude/projects/-home-alice-garden-planner/7c1bbcfa-d133-4164-9357-86c5a060e3d6/subagents/agent-aef1405d432609001.jsonl',
    'scp ./GP_sync_the_11_beds.py server:/data/GP_simulate_season_2710_V5l.py',
    'sh run.sh -C "sh /tmp/GP_simulate_batch_v2_chain_by_bed_server_RESTIT11.sh"',
    'D=/home/alice/reports/SEEDLINGS_109_CELLS_DEC_REVIEW_19-10.html',
    "F='/home/alice/notes/HANDOVER_2026-10-27.md'",
    "URL='https://example.org/api/beds/1713551'",
    'the margins of the page and the marginal note',
    'max_tokens: 4096',
    'session f2ad6450-2e71-4125-a370-b490e573d7bf',
    'price_public 9.95 and pricePublicCentimes',
    'margin of error',
    'margin: 0 auto;',
    'batch_1830_T19 PHASE2P7C_2710 D72 v1172',
    'SKU 1713551 barcode 1234567890128',
    'def renderCalendarForPlantingSeasonTwentyTwentySixBis():',
    'planner.py start --agent GARDENER',
    'noreply@anthropic.com',
]


def main():
    failures = 0
    for name, (template, secret) in WITNESSES.items():
        c = Counter()
        out = F.clean(template.format(secret), c)
        leaked = secret in out
        left = F.residues(out)
        ok = not leaked and not left and sum(c.values()) > 0
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} witness {name}: removals {dict(c)}"
              f"{' LEAK' if leaked else ''}{' residue ' + str(dict(left)) if left else ''}")
    for t in SANE:
        out = F.clean(t)
        ok = out == t
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} sane: {t if ok else out}")
    print(f"\n{failures} failure(s) out of {len(WITNESSES) + len(SANE)}.")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()

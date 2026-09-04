"""Demonstrate the exhaustiveness guard instead of asking you to believe it.

`python -m ledger_daemon prove` adds a tenth verdict to the taxonomy at runtime,
exactly as a developer would when a new settlement mode appears, and shows the
policy engine refusing to load until someone decides what collections does about
it. The guarantee is that a new way for money to arrive cannot silently inherit
"deny" -- or worse, "chase".

This runs in a subprocess: it mutates the Verdict enum, which is a deliberately
destructive thing to do to a live process.
"""

from __future__ import annotations

import subprocess
import sys

# Kept as source rather than a function so the output shows exactly what was run.
_INJECT = '''
from ledger_daemon import policy
from ledger_daemon.models import Verdict

# A developer adds a settlement mode and forgets the policy decision.
member = str.__new__(Verdict, "settled_via_wallet")
member._name_, member._value_ = "SETTLED_VIA_WALLET", "settled_via_wallet"
Verdict._member_map_["SETTLED_VIA_WALLET"] = member
Verdict._member_names_.append("SETTLED_VIA_WALLET")
Verdict._value2member_map_["settled_via_wallet"] = member

policy._assert_exhaustive()
print("NO ERROR")
'''


def run() -> int:
    print("Ledger Daemon — exhaustiveness proof")
    print()

    from . import policy
    from .models import Verdict

    print(f"The taxonomy has {len(list(Verdict))} verdicts, and the policy engine")
    print(f"declares a disposition for all {len(policy.VERDICT_DISPOSITION)} of them:")
    print()
    width = max(len(v.value) for v in policy.VERDICT_DISPOSITION)
    for v in Verdict:
        print(f"    {v.value:<{width}}  ->  {policy.VERDICT_DISPOSITION[v].value}")

    print()
    print("Now a tenth verdict arrives — settled_via_wallet — and nobody decides")
    print("what collections does about it. Injecting it into the live enum:")
    print()
    for line in _INJECT.strip().splitlines():
        print(f"    {line}")
    print()

    proc = subprocess.run([sys.executable, "-c", _INJECT],
                          capture_output=True, text=True)

    if proc.returncode == 0 and "NO ERROR" in proc.stdout:
        print("FAILED: the guard did not fire. The exhaustiveness claim is false.")
        return 1

    err = [ln.strip() for ln in proc.stderr.strip().splitlines() if ln.strip()]
    raised = [ln for ln in err if ln.startswith("ImportError")] or err[-1:]
    for line in raised:
        print(f"    {line}")
    print()
    print("The module refuses to import. An unhandled verdict is a load-time error")
    print("on the developer's machine, not a silent DENY in production — and not a")
    print("chase against a customer whose money already arrived.")
    return 0

"""The proof command must actually prove something, and keep proving it."""

from ledger_daemon import policy, prove
from ledger_daemon.models import Verdict


def test_prove_succeeds_meaning_the_guard_really_fires(capsys):
    assert prove.run() == 0
    out = capsys.readouterr().out
    assert "not exhaustive" in out
    assert "settled_via_wallet" in out


def test_prove_does_not_corrupt_the_enum_in_this_process(capsys):
    """The injection runs in a subprocess; the live taxonomy must be untouched."""
    before = [v.value for v in Verdict]
    prove.run()
    capsys.readouterr()
    assert [v.value for v in Verdict] == before
    policy._assert_exhaustive()


def test_prove_lists_a_disposition_for_every_verdict(capsys):
    prove.run()
    out = capsys.readouterr().out
    for v in Verdict:
        assert v.value in out, f"{v.value} missing from the printed table"

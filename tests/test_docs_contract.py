"""The documentation is held to the same standard as the code (F11).

Three failure modes this file exists to prevent, all of which have already
happened once in this repository:

  1. **A number goes stale.** The README claimed "73 tests" long after there
     were several hundred. The fix is structural: prose may not hard-code a
     count that a command can print, and any figure that does appear must be
     traceable to a judge artifact.
  2. **The framing drifts.** Track 04 is the primary track; recovery is a
     downstream demonstration. Both have exact wording, and it is checked.
  3. **A comparison creeps in.** No competitor names, no competitor repository
     URLs, no "us versus them" section. Every external link is on an allowlist.
"""

import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Documents the repository ships. A fresh clone must contain every one.
REQUIRED_DOCS = (
    "README.md", "ARCHITECTURE.md", "CLAIMS.md", "METHODS.md", "EVALUATION.md",
    "LIMITATIONS.md", "SECURITY.md", "SIMULATED_VS_REAL.md", "WHAT_BROKE.md",
)

#: Present in a working copy, excluded from the repository by
#: `.git/info/exclude`. They are held to the same contract when they are there,
#: and their absence is not a failure of this test -- but it does mean an
#: evaluator cloning the repository never reads them.
LOCAL_DOCS = ("DECISIONS.md", "BROKEN.md", "PLAN.md")

JUDGE_COMMAND = "python -m ledger_daemon judge --seed 42 --n 500 --out out/judge"

#: The only external links the submission carries, each with a reason.
URL_ALLOWLIST = {
    # the independent Fellegi-Sunter engine the matcher is cross-checked against
    "https://github.com/moj-analytical-services/splink",
    # the project's public GitHub Pages landing page and saved controller
    "https://jayatiahuja.github.io/Ledger-Daemon/",
    "https://jayatiahuja.github.io/Ledger-Daemon/demo.html",
}

_URL = re.compile(r"https?://[^\s)\]\"'>]+")
_TEST_COUNT = re.compile(r"\b\d{2,4}\s+tests\b", re.IGNORECASE)


def _read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _docs() -> dict[str, str]:
    """Every document present in this working copy, shipped or local."""
    names = list(REQUIRED_DOCS) + [
        name for name in LOCAL_DOCS if os.path.exists(os.path.join(ROOT, name))]
    return {name: _read(name) for name in names}


@pytest.mark.parametrize("name", REQUIRED_DOCS)
def test_required_document_exists_and_says_something(name):
    assert os.path.exists(os.path.join(ROOT, name)), f"{name} is missing"
    assert len(_read(name).strip()) > 400, f"{name} is a stub"


def test_track_04_is_the_only_primary_framing():
    readme = _read("README.md")
    assert "Track 04" in readme
    for name, body in _docs().items():
        assert "Track 03" not in body, f"{name} describes the project as Track 03"


def test_recovery_is_labelled_exactly_as_a_downstream_demonstration():
    label = "Downstream Control Demonstration: Why Correct Reconciliation Matters"
    assert label in _read("README.md")
    assert label in _read("EVALUATION.md")


def test_the_judge_command_is_quoted_exactly():
    for name in ("README.md", "CLAIMS.md", "EVALUATION.md"):
        assert JUDGE_COMMAND in _read(name), f"{name} does not carry the judge command"


def test_the_headline_points_at_the_claims_file():
    head = _read("README.md")[:2000]
    assert "CLAIMS.md" in head, "the headline number must link to where it came from"


def test_synthetic_labels_are_disclosed_where_numbers_appear():
    for name in ("README.md", "CLAIMS.md", "EVALUATION.md", "LIMITATIONS.md"):
        body = _read(name).lower()
        assert "synthetic" in body, f"{name} quotes numbers without naming the data class"
    assert "written at injection time" in _read("LIMITATIONS.md").lower()


def test_no_document_hard_codes_a_test_count():
    """A count in prose is a number nobody re-runs. The command prints it."""
    for name, body in _docs().items():
        found = _TEST_COUNT.findall(body)
        assert not found, f"{name} hard-codes a test count: {found}"


def test_every_external_link_is_on_the_allowlist():
    for name, body in _docs().items():
        for url in _URL.findall(body):
            clean = url.rstrip(".,;")
            assert clean in URL_ALLOWLIST, f"{name} links to {clean}, which is not allowlisted"


def test_no_comparison_section_against_named_products():
    banned = ("vs.", " versus ", "competitor", "alternatives to")
    for name, body in _docs().items():
        lowered = body.lower()
        for phrase in banned:
            if phrase in lowered:
                # allowed only when comparing this system's own configurations
                for line in lowered.splitlines():
                    if phrase in line:
                        assert any(token in line for token in
                                   ("b0", "b1", "b2", "r0", "r1", "r2", "r3", "r4",
                                    "baseline", "ablation", "exact", "profile")), (
                            f"{name} carries a product comparison: {line.strip()[:90]}")


def test_limitations_names_the_five_things_that_are_not_proven():
    body = _read("LIMITATIONS.md").lower()
    for needle in ("no real merchant", "exchangeab", "authored", "optional",
                   "coverage"):
        assert needle in body, f"LIMITATIONS.md does not address {needle!r}"


def test_security_covers_the_boundary_and_the_secrets():
    body = _read("SECURITY.md").lower()
    for needle in ("pii", "test mode", "prompt injection", "append-only", "sha-256"):
        assert needle in body, f"SECURITY.md does not address {needle!r}"


def test_simulated_vs_real_separates_the_four_data_classes():
    body = _read("SIMULATED_VS_REAL.md").lower()
    for needle in ("synthetic", "schema-derived", "test mode api", "merchant-provided"):
        assert needle in body, f"SIMULATED_VS_REAL.md does not name {needle!r}"
    assert "no credentialed test mode run" in body
    assert "no production money" in body


def test_claims_table_is_complete():
    body = _read("CLAIMS.md")
    rows = [line for line in body.splitlines()
            if line.startswith("|") and "---" not in line]
    assert len(rows) > 4, "CLAIMS.md carries no claim rows"
    header = [cell.strip().lower() for cell in rows[0].strip("|").split("|")]
    for column in ("claim", "value", "dataset class", "profile", "seed", "n",
                   "command", "artifact", "limitation"):
        assert column in header, f"CLAIMS.md has no {column!r} column"
    for row in rows[1:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) == len(header), f"ragged claim row: {row[:80]}"
        assert all(cells), f"claim row has an empty cell: {row[:80]}"


def _judge_summary() -> dict | None:
    for candidate in ("out/judge/summary.json", "out/release-judge/summary.json",
                      "out/ci-judge/summary.json"):
        path = os.path.join(ROOT, candidate)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    return None


def test_claims_agree_with_the_judge_artifact_when_one_is_present():
    summary = _judge_summary()
    if summary is None:
        pytest.skip("no judge artifact in this checkout; CI generates one before pytest")
    body = _read("CLAIMS.md")
    assert summary["fingerprint"][:16] in body, (
        "CLAIMS.md does not name the run its numbers came from")
    assert str(summary["wrongly_chased_paise"]) in body
    assert str(summary["duplicate_side_effects"]) in body


def test_readme_headline_matches_the_judge_artifact_when_one_is_present():
    summary = _judge_summary()
    if summary is None:
        pytest.skip("no judge artifact in this checkout")
    clean = next(p for p in summary["profiles"] if p["profile"] == "clean")
    readme = _read("README.md")
    assert f"{clean['dcpr_ppm'] / 10_000:.1f}%" in readme
    assert f"{clean['false_hold_ppm'] / 10_000:.1f}%" in readme

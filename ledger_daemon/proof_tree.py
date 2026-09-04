"""The certificate, drawn for a human (F2).

`verify-proof` answers VALID or INVALID. That is the right answer for a
machine and the wrong one for the analyst who has to defend a decision to a
customer, so this module renders the same certificate as a tree: what was
claimed, what it was computed from, and what the gate did about it.

Two rules keep the tree honest:

* **Nothing but the certificate.** Every label, digest and amount below comes
  out of `ProofCertificate` fields. There is no model-written explanation here
  and no second source of truth -- the tree cannot say something the proof does
  not already say, which is what makes it usable as evidence.
* **Recomputed or marked as a claim.** The proof hash and the amount equation
  are checked here and reported PASS/FAIL. Source integrity, calibration and
  config identity cannot be settled without the source files, so they are drawn
  as CLAIM and named as the verifier's job -- `verify-proof` is what turns them
  into a verdict. A tree that quietly reported them green would be theatre.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .certificates import ProofCertificate
from .models import Verdict
from .money import rupees_str
from .policy import VERDICT_DISPOSITION
from .source_contracts import sha256_hex

PASS = "PASS"      # recomputed here, from the certificate alone
FAIL = "FAIL"      # recomputed here, and it does not hold
CLAIM = "CLAIM"    # asserted by the proof; verify-proof settles it against the sources
INFO = "INFO"      # a fact being displayed, not a check


@dataclass(frozen=True)
class ProofNode:
    label: str
    status: str = INFO
    detail: str = ""
    children: tuple["ProofNode", ...] = field(default_factory=tuple)


def _rollup(children: tuple[ProofNode, ...]) -> str:
    """A section is only as sound as its weakest child.

    A section of unverified claims must not print as PASS. Marking `source
    integrity` green when this module has never opened a source file would be
    the exact theatre a proof tree exists to replace, so one CLAIM child pulls
    the whole section down to CLAIM, and one FAIL pulls it to FAIL.
    """
    statuses = {child.status for child in children}
    if FAIL in statuses:
        return FAIL
    if CLAIM in statuses:
        return CLAIM
    return PASS if PASS in statuses else INFO


def _section(label: str, children: tuple[ProofNode, ...], detail: str = "") -> ProofNode:
    return ProofNode(label, _rollup(children), detail, children)


# ------------------------------ the sections -------------------------------- #

def _integrity(certificate: ProofCertificate) -> ProofNode:
    recomputed = sha256_hex(certificate._payload())
    holds = recomputed == certificate.proof_hash
    return _section("proof integrity", (
        ProofNode("proof hash", PASS if holds else FAIL,
                  certificate.proof_hash if holds
                  else f"{certificate.proof_hash} != recomputed {recomputed}"),
        ProofNode("certificate version", INFO, certificate.version),
    ))


def _sources(certificate: ProofCertificate) -> ProofNode:
    children = tuple(
        ProofNode(row_id, CLAIM, digest)
        for row_id, digest in certificate.source_hashes
    )
    return _section(
        "source integrity", children,
        f"{len(children)} rows bound by hash — "
        "`verify-proof` recomputes these from the source files")


def _amounts(certificate: ProofCertificate) -> ProofNode:
    terms = tuple(
        ProofNode(
            term.name, INFO,
            (f"{'+' if term.amount_paise >= 0 else '-'}"
             f"{rupees_str(abs(term.amount_paise))}"
             + (f"   {term.source_row_id}.{term.source_field}" if term.source_row_id else "")),
        )
        for term in certificate.amount_terms
    )
    total = sum(term.amount_paise for term in certificate.amount_terms)
    # An empty term list is the honest shape for a verdict that claims no
    # arithmetic (ambiguous, failed_not_debited); zero is not a failure there.
    balances = total == 0
    children = terms + (
        ProofNode("sum of signed terms", PASS if balances else FAIL,
                  f"{'+' if total >= 0 else '-'}{rupees_str(abs(total))}"
                  + ("" if balances else "  — the equation does not close")),
        ProofNode("money received", INFO, rupees_str(certificate.money_received_paise)),
        ProofNode("delta due", INFO, rupees_str(certificate.delta_due_paise)),
    )
    return _section("amount identity", children, "integer paise; signed terms must sum to zero")


def _rules(certificate: ProofCertificate) -> ProofNode:
    return _section("rules applied", tuple(
        ProofNode(rule, INFO, "") for rule in certificate.rule_ids))


def _calibration(certificate: ProofCertificate) -> ProofNode:
    return _section("calibration and configuration", (
        ProofNode("calibration_id", CLAIM, certificate.calibration_id),
        ProofNode("config_hash", CLAIM, certificate.config_hash),
        ProofNode("generated_at", INFO, certificate.generated_at),
    ), "the abstention threshold and engine switches this verdict was decided under")


_CONSEQUENCE = {
    "CHASE": "the only states collections may act on",
    "BLOCK_ALREADY_PAID": "the money is provably in; chasing it is the harm",
    "BLOCK_NOT_A_DEBT": "never debited — a retry, not a dunning case",
    "BLOCK_STATUTORY_DEDUCTION": "the shortfall is withheld tax, not an unpaid invoice",
    "FREEZE_ESCALATE": "a dispute is in flight; collections must freeze",
    "ABSTAIN_FOR_HUMAN": "not classifiable at the calibrated error rate; opens a case",
}


def _consequence(certificate: ProofCertificate) -> ProofNode:
    """What the gate does with this verdict -- read off policy's own table."""
    try:
        disposition = VERDICT_DISPOSITION[Verdict(certificate.verdict)].value
    except ValueError:
        return ProofNode("policy consequence", FAIL, "", (
            ProofNode("R1 disposition", FAIL,
                      f"{certificate.verdict} is not in the verdict taxonomy"),))
    return _section("policy consequence", (
        ProofNode("R1 disposition", INFO,
                  f"{disposition} — {_CONSEQUENCE[disposition]}"),
    ), "policy.VERDICT_DISPOSITION, the same table the gate runs")


def certificate_to_tree(certificate: ProofCertificate) -> ProofNode:
    """Render one certificate as a tree. Certificate fields only."""
    sections = (
        _integrity(certificate),
        _sources(certificate),
        _amounts(certificate),
        _rules(certificate),
        _calibration(certificate),
        _consequence(certificate),
    )
    return ProofNode(
        f"{certificate.order_id} — {certificate.verdict}",
        _rollup(sections),
        certificate.proof_hash,
        sections,
    )


# ------------------------------ rendering ----------------------------------- #

_MARK = {PASS: "[ok]", FAIL: "[FAIL]", CLAIM: "[claim]", INFO: "     "}


def render_text(tree: ProofNode) -> str:
    lines: list[str] = []

    def draw(node: ProofNode, prefix: str, connector: str) -> None:
        head = f"{prefix}{connector}{node.label}"
        lines.append(f"{head:<58} {_MARK[node.status]:<7} {node.detail}".rstrip())
        below = prefix + ("   " if connector in ("`- ", "") else "|  ")
        for index, child in enumerate(node.children):
            last = index == len(node.children) - 1
            draw(child, below, "`- " if last else "|- ")

    draw(tree, "", "")
    return "\n".join(lines)


def load_certificates(proofs_dir: str) -> dict[str, ProofCertificate]:
    """Read an issued proof bundle. Missing or unreadable -> no certificates.

    The CLI and the workbench both read the bundle rather than rebuilding it,
    which is what makes them show the same proof hash as `verify-proof`.
    """
    certificates: dict[str, ProofCertificate] = {}
    try:
        filenames = sorted(os.listdir(proofs_dir))
    except OSError:
        return certificates
    for filename in filenames:
        if not filename.endswith(".json") or filename == "proof-manifest.json":
            continue
        try:
            with open(os.path.join(proofs_dir, filename), encoding="utf-8") as fh:
                certificate = ProofCertificate.from_json(fh.read())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        certificates[certificate.order_id] = certificate
    return certificates

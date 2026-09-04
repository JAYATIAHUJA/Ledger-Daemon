"""A proof a human can read must be the same proof the verifier reads.

The tree is a rendering of the certificate and nothing else: no prose written
by a model, no field the certificate does not carry. Every claim it draws is
either recomputed on the spot (the proof hash, the amount equation) or marked
as a claim the independent verifier settles against the source files.
"""

import re

import pytest

from ledger_daemon.certificates import (
    batch_root,
    calibration_identity,
    recon_config_hash,
    source_hash_map,
    source_rows,
    write_proof_bundle,
)
from ledger_daemon.datagen import generate, load_batch
from ledger_daemon.money import rupees_str
from ledger_daemon.proof_tree import (
    CLAIM,
    FAIL,
    PASS,
    ProofNode,
    certificate_to_tree,
    load_certificates,
    render_text,
)
from ledger_daemon.recon import FULL, reconcile


@pytest.fixture(scope="module")
def proofs(tmp_path_factory):
    """One real proof bundle, issued the way the demo issues it."""
    root = tmp_path_factory.mktemp("proof-tree")
    batch = str(root / "batch")
    generate(11, 120, batch)
    orders, captures, bank, _truth = load_batch(batch)
    result = reconcile(orders, captures, bank, q_hat=0.001)
    rows = source_rows(orders, captures, bank)
    write_proof_bundle(
        str(root / "proofs"), orders, captures, bank, result.verdicts,
        config_hash=recon_config_hash(FULL),
        calibration_id=calibration_identity(0.001, batch_root(source_hash_map(rows))),
    )
    certificates = load_certificates(str(root / "proofs"))
    return certificates, result.verdicts


def _by_verdict(proofs, verdict: str):
    certificates, verdicts = proofs
    for order_id, v in sorted(verdicts.items()):
        if v.verdict.value == verdict and order_id in certificates:
            return certificates[order_id]
    pytest.skip(f"batch produced no {verdict} certificate")


def _labels(node: ProofNode) -> set[str]:
    return {node.label} | {
        label for child in node.children for label in _labels(child)}


def _find(node: ProofNode, label: str) -> ProofNode:
    if node.label == label:
        return node
    for child in node.children:
        found = _find(child, label)
        if found is not None:
            return found
    return None


# --------------------------- the required sections -------------------------- #

def test_the_tree_carries_every_section_a_reviewer_needs(proofs):
    certificate = _by_verdict(proofs, "settled_clean")
    tree = certificate_to_tree(certificate)
    assert {"proof integrity", "source integrity", "amount identity",
            "calibration and configuration", "policy consequence"} <= _labels(tree)


def test_source_integrity_names_the_actual_rows_and_digests(proofs):
    certificate = _by_verdict(proofs, "settled_clean")
    section = _find(certificate_to_tree(certificate), "source integrity")
    drawn = {child.label: child.detail for child in section.children}
    assert drawn == {row_id: digest for row_id, digest in certificate.source_hashes}
    assert "BATCH_ROOT" in drawn
    assert certificate.order_id in drawn
    # The tree holds no source files, so integrity is a claim, not a verdict.
    assert all(child.status == CLAIM for child in section.children)


def test_amount_identity_shows_the_terms_and_recomputes_the_sum(proofs):
    certificate = _by_verdict(proofs, "settled_clean")
    section = _find(certificate_to_tree(certificate), "amount identity")
    drawn = section.children[:len(certificate.amount_terms)]
    assert [child.label for child in drawn] == [t.name for t in certificate.amount_terms]
    for child, term in zip(drawn, certificate.amount_terms):
        assert rupees_str(abs(term.amount_paise)) in child.detail
        if term.source_row_id:
            assert f"{term.source_row_id}.{term.source_field}" in child.detail
    total = _find(section, "sum of signed terms")
    assert total.status == PASS
    assert "0.00" in total.detail


def test_calibration_section_carries_the_identities_verbatim(proofs):
    certificate = _by_verdict(proofs, "settled_clean")
    section = _find(certificate_to_tree(certificate), "calibration and configuration")
    details = {child.label: child.detail for child in section.children}
    assert details["calibration_id"] == certificate.calibration_id
    assert details["config_hash"] == certificate.config_hash
    assert details["generated_at"] == certificate.generated_at


def test_the_policy_consequence_is_the_last_thing_a_reader_sees(proofs):
    paid = certificate_to_tree(_by_verdict(proofs, "settled_clean"))
    unpaid = certificate_to_tree(_by_verdict(proofs, "genuinely_unpaid"))

    assert paid.children[-1].label == "policy consequence"
    assert "BLOCK_ALREADY_PAID" in _find(paid, "policy consequence").children[0].detail
    assert "CHASE" in _find(unpaid, "policy consequence").children[0].detail


# --------------------------- it is a rendering, not a story ----------------- #

def test_a_tampered_proof_hash_fails_the_integrity_node(proofs):
    from dataclasses import replace
    certificate = _by_verdict(proofs, "settled_clean")
    tampered = replace(certificate, proof_hash="0" * 64)

    tree = certificate_to_tree(tampered)
    assert _find(tree, "proof hash").status == FAIL
    assert tree.status == FAIL


def test_a_tampered_amount_breaks_the_equation_not_the_narrative(proofs):
    from dataclasses import replace
    certificate = _by_verdict(proofs, "settled_clean")
    terms = list(certificate.amount_terms)
    terms[0] = replace(terms[0], amount_paise=terms[0].amount_paise + 1)
    tampered = replace(certificate, amount_terms=tuple(terms))

    section = _find(certificate_to_tree(tampered), "amount identity")
    assert _find(section, "sum of signed terms").status == FAIL


def test_every_drawn_value_comes_from_the_certificate(proofs):
    """No node may introduce a hash, id or amount the certificate does not carry."""
    certificate = _by_verdict(proofs, "settled_clean")
    tree = certificate_to_tree(certificate)

    known = {
        certificate.order_id, certificate.verdict, certificate.proof_hash,
        certificate.config_hash, certificate.calibration_id, certificate.generated_at,
        certificate.version, *certificate.rule_ids,
        *(digest for _row, digest in certificate.source_hashes),
        *(row for row, _digest in certificate.source_hashes),
        *(term.name for term in certificate.amount_terms),
        *(term.source_row_id for term in certificate.amount_terms),
        *(term.source_field for term in certificate.amount_terms),
    }
    haystack = " ".join(str(value) for value in known)
    text = render_text(tree)
    for token in re.findall(r"[0-9a-f]{16,}", text):
        assert token in haystack, f"the tree invented the digest {token[:16]}"


def test_render_text_is_deterministic_and_shows_the_proof_hash(proofs):
    certificate = _by_verdict(proofs, "settled_clean")
    first = render_text(certificate_to_tree(certificate))
    second = render_text(certificate_to_tree(certificate))
    assert first == second
    assert certificate.proof_hash in first
    assert certificate.order_id in first.splitlines()[0]


def test_load_certificates_skips_the_manifest(proofs):
    certificates, verdicts = proofs
    assert len(certificates) == len(verdicts)
    assert "proof-manifest" not in certificates


# --------------------------- one proof, two surfaces ------------------------ #

def test_the_cli_and_the_workbench_render_the_same_proof(proofs, tmp_path):
    """Task 3 acceptance: explain and the HTML view show one certificate."""
    from ledger_daemon import policy
    from ledger_daemon.cli import render_proof
    from ledger_daemon.models import Order
    from ledger_daemon.ui import build_view

    certificates, verdicts = proofs
    certificate = _by_verdict(proofs, "settled_clean")
    order_id = certificate.order_id

    bundle = tmp_path / "proofs"
    bundle.mkdir()
    (bundle / f"{order_id}.json").write_text(certificate.to_json(), encoding="utf-8")

    # status "unpaid" keeps the row visible: a books-say-paid row is hidden as
    # a routine agreement, and this test is about what a reader can see.
    order = Order(order_id, "INV-1", "CUST-1", "MEHTA EXPORTS",
                  100, "2026-08-10", "unpaid", "gateway")
    decision = policy.Decision(policy.DENY, "R1_DENY_ALREADY_PAID", "already paid")
    view = build_view([order], {order_id: verdicts[order_id]},
                      {order_id: decision}, {}, None, certificates)
    row = (view.safe + view.blocked + view.needs_you)[0]

    assert row.proof_hash == certificate.proof_hash
    assert row.proof_tree == render_text(certificate_to_tree(certificate))
    assert row.proof_tree in render_proof(str(bundle), order_id)


def test_the_workbench_shows_no_proof_when_none_was_issued(proofs):
    from ledger_daemon import policy
    from ledger_daemon.models import Order
    from ledger_daemon.ui import build_view

    _certificates, verdicts = proofs
    order_id = _by_verdict(proofs, "settled_clean").order_id
    order = Order(order_id, "INV-1", "CUST-1", "MEHTA EXPORTS",
                  100, "2026-08-10", "unpaid", "gateway")
    decision = policy.Decision(policy.HOLD, "R1_HOLD_AMBIGUOUS", "")
    view = build_view([order], {order_id: verdicts[order_id]}, {order_id: decision}, {})
    assert view.needs_you[0].proof_tree == "" and view.needs_you[0].proof_hash == ""


def test_explain_says_so_when_the_bundle_is_missing(tmp_path):
    from ledger_daemon.cli import render_proof
    message = render_proof(str(tmp_path / "absent"), "ORD-1")
    assert "no issued proof for ORD-1" in message

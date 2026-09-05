"""PAID_NET_OF_TDS: a compliant B2B payer must never be dunned.

The scenario: the customer pays the invoice minus a statutory TDS rate by bank
transfer. Before this verdict existed, the exact-amount candidate gate never even
considered that credit, so the order came out GENUINELY_UNPAID and the payer got
chased for tax they had already deposited against the merchant's PAN.
"""

from ledger_daemon import policy
from ledger_daemon import certificates
from ledger_daemon import verifier
from ledger_daemon.datagen import generate, load_batch, load_finance_events
from ledger_daemon.finance_events import decode_finance_event
from ledger_daemon.models import BankTxn, Evidence, Order, OrderVerdict, Verdict
from ledger_daemon.money import pct_bp, sub, tds_rate_bp
from ledger_daemon.recon import reconcile


def _order(amount=100_000_00):
    return Order("ORD-1", "INV-2291", "CUST-1", "SHARMA TEXTILES PVT LTD",
                 amount, "2026-08-10", "unpaid", "bank_transfer")


def _credit(amount, narration="NEFT-HDFCP12345678-SHARMA TEXTILES PVT-INV2291"):
    return BankTxn("TXN-9", "2026-08-11", amount, "credit", "HDFCN777777777", narration, 0)


def _tds_evidence(amount=200_000):
    return decode_finance_event({
        "event_type": "tds_evidence",
        "evidence_id": "TDSE-1",
        "order_id": "ORD-1",
        "amount_paise": amount,
        "payer_pan_hash": "a" * 64,
        "merchant_pan_hash": "b" * 64,
        "tax_rule_id": "ITA2025:S393:PROFESSIONAL@2026-04-01",
        "certificate_ref": "FORM26AS:2026Q2:ACK-991",
        "occurred_at": "2026-08-10",
    })


# ---- the detector itself: exact statutory rates, no tolerance ----------------

def test_detector_recognises_each_statutory_rate():
    gross = 100_000_00
    for bp in (100, 200, 1000):
        net = sub(gross, pct_bp(gross, bp))
        assert tds_rate_bp(gross, net) == bp


def test_detector_rejects_non_statutory_shortfalls():
    gross = 100_000_00
    assert tds_rate_bp(gross, gross) is None                       # paid in full
    assert tds_rate_bp(gross, sub(gross, pct_bp(gross, 300))) is None   # 3% is not a rate
    assert tds_rate_bp(gross, sub(gross, pct_bp(gross, 200)) - 1) is None  # one paisa off
    assert tds_rate_bp(gross, 0) is None
    assert tds_rate_bp(gross, gross + 100) is None                 # overpayment


# ---- reconciliation: the net credit becomes a candidate and a verdict --------

def test_percentage_sized_shortfall_without_typed_evidence_is_held():
    o = _order()
    b = _credit(sub(o.amount_paise, pct_bp(o.amount_paise, 200)))  # 2% withheld
    v = reconcile([o], [], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.POSSIBLE_TDS_WITHHOLDING
    assert v.money_received_paise == 0
    assert "evidence" in v.reason.lower()


def test_matching_typed_tds_evidence_allows_paid_net_of_tds():
    o = _order()
    b = _credit(9_800_000)
    evidence = _tds_evidence()

    v = reconcile([o], [], [b], finance_events=[evidence]).verdicts["ORD-1"]

    assert v.verdict is Verdict.PAID_NET_OF_TDS
    assert v.money_received_paise == 9_800_000
    assert v.evidence_refs == ["TDSE-1", "TXN-9"]
    assert "verified TDS evidence" in v.reason


def test_typed_evidence_supports_rates_outside_legacy_candidate_list():
    o = _order()
    b = _credit(9_990_000)  # 0.1%; applicability comes from the external source
    evidence = _tds_evidence(amount=10_000)

    v = reconcile([o], [], [b], finance_events=[evidence]).verdicts["ORD-1"]

    assert v.verdict is Verdict.PAID_NET_OF_TDS
    assert v.money_received_paise == 9_990_000


def test_tds_evidence_with_wrong_amount_does_not_close_invoice():
    o = _order()
    b = _credit(9_000_000)
    evidence = _tds_evidence(amount=200_000)

    v = reconcile([o], [], [b], finance_events=[evidence]).verdicts["ORD-1"]

    assert v.verdict is Verdict.POSSIBLE_TDS_WITHHOLDING


def test_paid_net_of_tds_proof_binds_external_tax_evidence():
    o = _order()
    b = _credit(9_800_000)
    evidence = _tds_evidence()
    v = reconcile([o], [], [b], finance_events=[evidence]).verdicts["ORD-1"]
    rows = certificates.source_rows([o], [], [b], finance_events=[evidence])
    proof = certificates.build_certificate(
        o, v, certificates.source_hash_map(rows), "c" * 64, "cal:test", rows=rows,
    )

    checked = verifier.verify_certificate(
        proof, rows, expected_config_hash="c" * 64,
        expected_calibration_id="cal:test",
    )
    assert checked.valid, checked.error_codes
    tds_term = next(term for term in proof.amount_terms if term.name == "tds_withheld")
    assert (tds_term.amount_paise, tds_term.source_row_id, tds_term.source_field) == (
        -200_000, "TDSE-1", "amount_paise",
    )

    tampered_rows = [
        {**row, "amount_paise": 199_999}
        if row.get("evidence_id") == "TDSE-1" else row
        for row in rows
    ]
    rejected = verifier.verify_certificate(
        proof, tampered_rows, expected_config_hash="c" * 64,
        expected_calibration_id="cal:test",
    )
    assert not rejected.valid


def test_independent_verifier_rejects_raw_pan_even_when_certificate_hashes_match():
    o = _order()
    b = _credit(9_800_000)
    evidence = _tds_evidence()
    v = reconcile([o], [], [b], finance_events=[evidence]).verdicts["ORD-1"]
    valid_rows = certificates.source_rows([o], [], [b], finance_events=[evidence])
    valid = certificates.build_certificate(
        o, v, certificates.source_hash_map(valid_rows), "c" * 64, "cal:test",
        rows=valid_rows,
    )
    bad_rows = [
        {**row, "payer_pan_hash": "ABCDE1234F"}
        if row.get("evidence_id") == "TDSE-1" else row
        for row in valid_rows
    ]
    hashes = certificates.source_hash_map(bad_rows)
    forged = certificates.ProofCertificate.create(
        order_id=valid.order_id,
        verdict=valid.verdict,
        source_hashes={**hashes, "BATCH_ROOT": certificates.batch_root(hashes)},
        amount_terms=valid.amount_terms,
        money_received_paise=valid.money_received_paise,
        delta_due_paise=valid.delta_due_paise,
        rule_ids=valid.rule_ids,
        config_hash=valid.config_hash,
        calibration_id=valid.calibration_id,
        automation_path=valid.automation_path,
        score_ppm=valid.score_ppm,
        risk_calibration_id=valid.risk_calibration_id,
        risk_authorized=valid.risk_authorized,
        generated_at=valid.generated_at,
    )

    checked = verifier.verify_certificate(
        forged, bad_rows, expected_config_hash="c" * 64,
        expected_calibration_id="cal:test",
    )
    assert "TDS_EVIDENCE_INVALID" in checked.error_codes


def test_a_non_statutory_short_payment_stays_chaseable():
    """97% of the invoice is a short payment, not TDS — it must NOT be excused."""
    o = _order()
    b = _credit(sub(o.amount_paise, pct_bp(o.amount_paise, 300)))
    v = reconcile([o], [], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.GENUINELY_UNPAID


# ---- policy: the verdict can never reach the executor ------------------------

def test_policy_denies_with_a_tds_specific_rule():
    v = OrderVerdict("ORD-1", Verdict.PAID_NET_OF_TDS, [], Evidence("pass4_fuzzy"))
    d = policy.evaluate(_order(), v, "CREATE_PAYMENT_LINK", 0, 0)
    assert d.outcome == policy.DENY
    assert d.rule_fired == "R1_DENY_NET_OF_TDS"
    assert "Form 26AS" in d.detail


# ---- end to end on a generated world -----------------------------------------

def test_no_tds_payer_is_ever_chased_on_a_generated_batch(tmp_path):
    generate(7, 300, str(tmp_path))
    orders, captures, bank, truth = load_batch(str(tmp_path))
    events = load_finance_events(str(tmp_path))
    result = reconcile(orders, captures, bank, q_hat=0.001, finance_events=events)
    tds_ids = {oid for oid, t in truth.items() if t["true_verdict"] == "paid_net_of_tds"}
    assert tds_ids, "the generator must produce TDS cases"
    for o in orders:
        if o.order_id not in tds_ids:
            continue
        d = policy.evaluate(o, result.verdicts[o.order_id], "CREATE_PAYMENT_LINK", 0, 0)
        assert d.outcome != policy.ALLOW, \
            f"{o.order_id}: a compliant TDS payer was chased via {d.rule_fired}"

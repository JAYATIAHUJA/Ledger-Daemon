"""Independent verifier for proof certificates.

This module deliberately does not import the reconciliation or scoring engine.
It treats the certificate as an untrusted claim and recomputes its integrity,
source bindings, integer-paise equation, and bounded rule identities.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
import re

from .certificates import CERTIFICATE_VERSION, ProofCertificate
from .narration import invoice_in_narration, parse
from .source_contracts import sha256_hex


_VERDICTS = frozenset({
    "settled_clean", "settled_late", "paid_out_of_band", "refunded_then_repaid",
    "partially_paid", "paid_net_of_tds", "genuinely_unpaid", "failed_not_debited",
    "chargeback_open", "ambiguous",
})
_PASSES = frozenset({
    "pass1_exact_utr", "pass2_amount_date", "pass3_settlement_id", "pass4_fuzzy",
    "pass4_rejected",
    "gateway_status", "net_arithmetic", "exhausted", "none",
})
_ALLOWED_RULES = frozenset(
    {f"RECON.{name}" for name in _PASSES}
    | {f"VERDICT.{name}" for name in _VERDICTS}
)
_RULE_VERDICTS = {
    "pass1_exact_utr": frozenset({"settled_clean", "settled_late", "partially_paid"}),
    "pass2_amount_date": frozenset({"settled_clean", "settled_late", "partially_paid"}),
    "pass3_settlement_id": frozenset({"settled_clean", "settled_late", "partially_paid"}),
    "pass4_fuzzy": frozenset({
        "paid_out_of_band", "paid_net_of_tds", "refunded_then_repaid", "ambiguous",
    }),
    "pass4_rejected": frozenset({"genuinely_unpaid"}),
    "gateway_status": frozenset({"chargeback_open", "failed_not_debited"}),
    "net_arithmetic": frozenset({"ambiguous"}),
    "exhausted": frozenset({"genuinely_unpaid"}),
    "none": frozenset({"ambiguous"}),
}


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    error_codes: tuple[str, ...]


def _row_id(row: dict[str, object]) -> str | None:
    identifiers = [row.get(name) for name in ("order_id", "payment_id", "txn_id") if name in row]
    # A capture has both payment_id and order_id; its own primary key is payment_id.
    if "payment_id" in row:
        return row["payment_id"] if isinstance(row["payment_id"], str) else None
    if "txn_id" in row:
        return row["txn_id"] if isinstance(row["txn_id"], str) else None
    if len(identifiers) == 1 and isinstance(identifiers[0], str):
        return identifiers[0]
    return None


def _plausible_bank_owner(order: dict[str, object], bank_row: dict[str, object]) -> bool:
    narration = str(bank_row.get("narration", ""))
    if invoice_in_narration(str(order.get("invoice_no", "")), narration):
        return True
    generic = {
        "AND", "CO", "CONSULTING", "ENTERPRISES", "INDUSTRIES", "LLP", "LTD",
        "PVT", "SERVICES", "SOLUTIONS", "TECH", "TRADERS",
    }
    order_tokens = [
        token for token in re.findall(r"[A-Z0-9]+", str(order.get("customer_name", "")).upper())
        if len(token) >= 3 and token not in generic
    ]
    bank_tokens = re.findall(r"[A-Z0-9]+", narration.upper())
    if not order_tokens or not bank_tokens:
        return True  # no discriminative identity signal: cannot safely prove "unpaid"
    return any(
        max(SequenceMatcher(None, token, candidate).ratio() for candidate in bank_tokens) >= 0.75
        for token in order_tokens
    )


def _verify_certificate(
    certificate: ProofCertificate,
    source_rows: list[dict[str, object]],
    *,
    expected_config_hash: str | None = None,
    expected_calibration_id: str | None = None,
) -> VerificationResult:
    errors: list[str] = []

    def reject(code: str) -> None:
        if code not in errors:
            errors.append(code)

    if certificate.version != CERTIFICATE_VERSION:
        reject("UNSUPPORTED_VERSION")
    if sha256_hex(certificate._payload()) != certificate.proof_hash:
        reject("PROOF_HASH_MISMATCH")
    if certificate.automation_path not in {"exact", "probabilistic", "manual"}:
        reject("RISK_PROVENANCE_INVALID")
    if type(certificate.score_ppm) is not int or not 0 <= certificate.score_ppm <= 1_000_000:
        reject("RISK_PROVENANCE_INVALID")
    if type(certificate.risk_calibration_id) is not str or type(certificate.risk_authorized) is not bool:
        reject("RISK_PROVENANCE_INVALID")
    elif certificate.automation_path == "probabilistic" and (
            certificate.risk_authorized and not certificate.risk_calibration_id):
        reject("RISK_PROVENANCE_INVALID")
    elif certificate.automation_path in {"exact", "manual"} and (
            certificate.risk_calibration_id or certificate.score_ppm != 0):
        reject("RISK_PROVENANCE_INVALID")

    indexed: dict[str, dict[str, object]] = {}
    duplicates: set[str] = set()
    for row in source_rows:
        row_id = _row_id(row)
        if row_id is None:
            reject("SOURCE_SCHEMA_INVALID")
            continue
        if row_id in indexed:
            duplicates.add(row_id)
        else:
            indexed[row_id] = row
    if duplicates:
        reject("DUPLICATE_SOURCE_CONSUMPTION")

    claimed_hashes = dict(certificate.source_hashes)
    claimed_root = claimed_hashes.pop("BATCH_ROOT", None)
    actual_hashes = {row_id: sha256_hex(row) for row_id, row in indexed.items()}
    actual_root = sha256_hex({key: actual_hashes[key] for key in sorted(actual_hashes)})
    if not isinstance(claimed_root, str) or claimed_root != actual_root:
        reject("BATCH_ROOT_MISMATCH")
    if certificate.order_id not in claimed_hashes:
        reject("ORDER_SOURCE_MISSING")
    for row_id, expected_hash in claimed_hashes.items():
        row = indexed.get(row_id)
        if row is None:
            reject("SOURCE_MISSING")
        elif sha256_hex(row) != expected_hash:
            reject("SOURCE_HASH_MISMATCH")

    for term in certificate.amount_terms:
        if type(term.amount_paise) is not int:
            reject("NON_INTEGER_MONEY")
        if term.source_row_id:
            row = indexed.get(term.source_row_id)
            if row is None or term.source_row_id not in claimed_hashes:
                reject("AMOUNT_TERM_SOURCE_MISSING")
                continue
            source_amount = row.get(term.source_field)
            if type(source_amount) is not int or abs(term.amount_paise) != abs(source_amount):
                reject("AMOUNT_TERM_SOURCE_MISMATCH")
        elif term.name == "delta_due" and term.amount_paise != -certificate.delta_due_paise:
            reject("AMOUNT_TERM_SOURCE_MISMATCH")
        elif term.name == "money_received" and term.amount_paise != -certificate.money_received_paise:
            reject("AMOUNT_TERM_SOURCE_MISMATCH")

    order = indexed.get(certificate.order_id)
    invoice = order.get("amount_paise") if order else None
    if type(invoice) is int:
        if certificate.verdict == "genuinely_unpaid":
            expected = {"invoice": invoice, "unpaid_exposure": -invoice}
            if {term.name: term.amount_paise for term in certificate.amount_terms} != expected:
                reject("VERDICT_AMOUNT_MISMATCH")
        elif certificate.verdict == "paid_net_of_tds":
            withheld = invoice - certificate.money_received_paise
            if not any(term.name == "tds_withheld" and term.amount_paise == -withheld
                       for term in certificate.amount_terms):
                reject("VERDICT_AMOUNT_MISMATCH")
        elif certificate.verdict == "partially_paid":
            if invoice != certificate.money_received_paise + certificate.delta_due_paise:
                reject("VERDICT_AMOUNT_MISMATCH")
        elif certificate.verdict not in {"ambiguous", "failed_not_debited"}:
            if invoice != certificate.money_received_paise:
                reject("VERDICT_AMOUNT_MISMATCH")

    if certificate.amount_terms and sum(term.amount_paise for term in certificate.amount_terms) != 0:
        reject("AMOUNT_EQUATION_FAILED")

    if certificate.verdict not in _VERDICTS:
        reject("VERDICT_INVARIANT_FAILED")
    if type(certificate.money_received_paise) is not int or type(certificate.delta_due_paise) is not int:
        reject("NON_INTEGER_MONEY")
    elif certificate.money_received_paise < 0 or certificate.delta_due_paise < 0:
        reject("VERDICT_INVARIANT_FAILED")
    elif certificate.verdict == "partially_paid":
        if certificate.money_received_paise <= 0 or certificate.delta_due_paise <= 0:
            reject("VERDICT_INVARIANT_FAILED")
    elif certificate.verdict in {
        "settled_clean", "settled_late", "paid_out_of_band", "refunded_then_repaid",
        "paid_net_of_tds", "chargeback_open",
    }:
        if certificate.money_received_paise <= 0 or certificate.delta_due_paise != 0:
            reject("VERDICT_INVARIANT_FAILED")
    elif certificate.money_received_paise != 0 or certificate.delta_due_paise != 0:
        reject("VERDICT_INVARIANT_FAILED")

    if any(rule not in _ALLOWED_RULES for rule in certificate.rule_ids):
        reject("RULE_NOT_ALLOWED")
    verdict_rules = [rule for rule in certificate.rule_ids if rule.startswith("VERDICT.")]
    if verdict_rules != [f"VERDICT.{certificate.verdict}"]:
        reject("VERDICT_RULE_MISMATCH")
    if len([rule for rule in certificate.rule_ids if rule.startswith("RECON.")]) != 1:
        reject("RECON_RULE_MISMATCH")

    recon_rules = [rule.removeprefix("RECON.") for rule in certificate.rule_ids
                   if rule.startswith("RECON.")]
    recon_rule = recon_rules[0] if len(recon_rules) == 1 else ""
    if certificate.verdict not in _RULE_VERDICTS.get(recon_rule, frozenset()):
        reject("RULE_VERDICT_MISMATCH")
    claimed_rows = [indexed[row_id] for row_id in claimed_hashes if row_id in indexed]
    claimed_captures = [row for row in claimed_rows if "payment_id" in row]
    claimed_credits = [row for row in claimed_rows
                       if "txn_id" in row and row.get("credit_debit") == "credit"]
    full_captures = [row for row in indexed.values()
                     if "payment_id" in row and row.get("order_id") == certificate.order_id]

    expected_terms: list[tuple[str, int, str, str]] = []
    if type(invoice) is int:
        if certificate.verdict == "genuinely_unpaid":
            expected_terms.extend((
                ("invoice", invoice, certificate.order_id, "amount_paise"),
                ("unpaid_exposure", -invoice, "", ""),
            ))
        elif certificate.verdict not in {"ambiguous", "failed_not_debited"}:
            expected_terms.extend((
                ("invoice", invoice, certificate.order_id, "amount_paise"),
                ("money_received", -certificate.money_received_paise, "", ""),
            ))
            if certificate.verdict == "partially_paid":
                expected_terms.append(("delta_due", -certificate.delta_due_paise, "", ""))
            elif certificate.verdict == "paid_net_of_tds":
                expected_terms.append((
                    "tds_withheld", -(invoice - certificate.money_received_paise), "", ""
                ))

    if recon_rule in {"pass1_exact_utr", "pass2_amount_date", "pass3_settlement_id"}:
        for row in sorted(claimed_captures, key=lambda item: str(item.get("payment_id"))):
            expected_terms.extend((
                ("gateway_refund" if row.get("status") == "refund" else "gateway_capture",
                 row.get("amount_paise"), row.get("payment_id"), "amount_paise"),
                ("gateway_fee", -row.get("fee_paise"), row.get("payment_id"), "fee_paise"),
                ("gateway_gst", -row.get("tax_paise"), row.get("payment_id"), "tax_paise"),
            ))
        for row in sorted(claimed_credits, key=lambda item: str(item.get("txn_id"))):
            expected_terms.append((
                "bank_credit", -row.get("amount_paise"), row.get("txn_id"), "amount_paise"
            ))
    elif recon_rule == "pass4_fuzzy" and certificate.verdict != "ambiguous":
        for row in sorted(claimed_credits, key=lambda item: str(item.get("txn_id"))):
            expected_terms.extend((
                ("bank_credit", row.get("amount_paise"), row.get("txn_id"), "amount_paise"),
                ("evidence_received", -certificate.money_received_paise, "", ""),
            ))
        if certificate.verdict == "refunded_then_repaid":
            for row in sorted(claimed_captures, key=lambda item: str(item.get("payment_id"))):
                expected_terms.extend((
                    ("gateway_refund" if row.get("status") == "refund" else "gateway_capture",
                     row.get("amount_paise"), row.get("payment_id"), "amount_paise"),
                    ("gateway_fee", -row.get("fee_paise"), row.get("payment_id"), "fee_paise"),
                    ("gateway_gst", -row.get("tax_paise"), row.get("payment_id"), "tax_paise"),
                ))

    actual_terms = Counter(
        (term.name, term.amount_paise, term.source_row_id, term.source_field)
        for term in certificate.amount_terms
    )
    if Counter(expected_terms) != actual_terms:
        reject("AMOUNT_TERM_SCHEMA_INVALID")

    if recon_rule in {"pass1_exact_utr", "pass2_amount_date", "pass3_settlement_id"}:
        if not claimed_captures or len(claimed_credits) != 1:
            reject("EVIDENCE_SHAPE_MISMATCH")
        elif any(type(row.get(field)) is not int
                 for row in claimed_captures for field in ("amount_paise", "fee_paise", "tax_paise")):
            reject("EVIDENCE_SHAPE_MISMATCH")
        else:
            settlement_ids = {str(row.get("settlement_id")) for row in claimed_captures
                              if row.get("settlement_id")}
            complete_group_ids = {
                str(row.get("payment_id")) for row in indexed.values()
                if "payment_id" in row and str(row.get("settlement_id")) in settlement_ids
            }
            claimed_capture_ids = {str(row.get("payment_id")) for row in claimed_captures}
            if (len(settlement_ids) != 1
                    or not any(row.get("order_id") == certificate.order_id for row in claimed_captures)
                    or claimed_capture_ids != complete_group_ids):
                reject("SETTLEMENT_MEMBERSHIP_MISMATCH")
            net_settlement = sum(
                row["amount_paise"] - row["fee_paise"] - row["tax_paise"]
                for row in claimed_captures
            )
            if net_settlement != claimed_credits[0].get("amount_paise"):
                reject("SETTLEMENT_EQUATION_FAILED")
            related = [row for row in claimed_captures if row.get("order_id") == certificate.order_id]
            refunded = sum(-row["amount_paise"] for row in related if row.get("status") == "refund")
            expected_received = invoice - refunded if type(invoice) is int else None
            if expected_received != certificate.money_received_paise:
                reject("EVIDENCE_AMOUNT_MISMATCH")
            credit = claimed_credits[0]
            if recon_rule == "pass1_exact_utr" and not any(
                row.get("utr") and row.get("utr") == credit.get("utr")
                for row in claimed_captures
            ):
                reject("RULE_EVIDENCE_MISMATCH")
            if recon_rule == "pass2_amount_date":
                try:
                    latest = max(date.fromisoformat(str(row.get("captured_at")))
                                 for row in claimed_captures)
                    delay = (date.fromisoformat(str(credit.get("value_date"))) - latest).days
                except ValueError:
                    delay = -1
                if not 0 <= delay <= 3:
                    reject("RULE_EVIDENCE_MISMATCH")
            if recon_rule == "pass3_settlement_id":
                settlement_ids = {row.get("settlement_id") for row in claimed_captures}
                parsed_settlement = parse(str(credit.get("narration", ""))).get("settlement_id")
                if len(settlement_ids) != 1 or parsed_settlement != next(iter(settlement_ids), None):
                    reject("RULE_EVIDENCE_MISMATCH")
            if certificate.verdict in {"settled_clean", "settled_late"}:
                latest_text = max(str(row.get("captured_at")) for row in claimed_captures
                                  if row.get("order_id") == certificate.order_id)
                delay = (date.fromisoformat(str(credit.get("value_date")))
                         - date.fromisoformat(latest_text)).days
                if ((certificate.verdict == "settled_clean" and delay > 1)
                        or (certificate.verdict == "settled_late" and delay <= 1)):
                    reject("VERDICT_EVIDENCE_MISMATCH")
    elif recon_rule == "pass4_fuzzy" and certificate.verdict != "ambiguous":
        if len(claimed_credits) != 1:
            reject("EVIDENCE_SHAPE_MISMATCH")
        elif claimed_credits[0].get("amount_paise") != certificate.money_received_paise:
            reject("EVIDENCE_AMOUNT_MISMATCH")
        if certificate.verdict == "paid_net_of_tds" and type(invoice) is int:
            allowed_nets = {invoice - (invoice * rate // 10_000) for rate in (100, 200, 1_000)}
            if certificate.money_received_paise not in allowed_nets:
                reject("TDS_RATE_NOT_ALLOWED")
        if certificate.verdict in {"paid_out_of_band", "paid_net_of_tds"} and full_captures:
            reject("VERDICT_EVIDENCE_MISMATCH")
        if certificate.verdict == "refunded_then_repaid":
            claimed_ids = {row.get("payment_id") for row in claimed_captures}
            full_ids = {row.get("payment_id") for row in full_captures}
            net = sum(row.get("amount_paise", 0) - row.get("fee_paise", 0)
                      - row.get("tax_paise", 0) for row in full_captures)
            if (claimed_ids != full_ids
                    or not any(row.get("status") == "refund" for row in full_captures)
                    or net != 0):
                reject("REFUND_EVIDENCE_MISMATCH")
    elif recon_rule == "pass4_rejected":
        if (certificate.automation_path != "probabilistic"
                or certificate.risk_authorized):
            reject("RISK_PROVENANCE_INVALID")
        if not claimed_credits:
            reject("EVIDENCE_SHAPE_MISMATCH")
        elif type(invoice) is not int:
            reject("EVIDENCE_AMOUNT_MISMATCH")
        else:
            possible_amounts = {invoice} | {
                invoice - (invoice * rate // 10_000) for rate in (100, 200, 1_000)
            }
            if any(credit.get("amount_paise") not in possible_amounts
                   for credit in claimed_credits):
                reject("EVIDENCE_AMOUNT_MISMATCH")
    elif recon_rule == "gateway_status":
        related_statuses = {
            row.get("status") for row in claimed_captures
            if row.get("order_id") == certificate.order_id
        }
        required = "chargeback_open" if certificate.verdict == "chargeback_open" else "failed"
        if required not in related_statuses:
            reject("EVIDENCE_STATUS_MISMATCH")
    elif recon_rule == "exhausted":
        if full_captures:
            reject("NEGATIVE_EVIDENCE_CONTRADICTED")
        if type(invoice) is int and order is not None:
            possible_amounts = {invoice} | {
                invoice - (invoice * rate // 10_000) for rate in (100, 200, 1_000)
            }
            for row in indexed.values():
                if ("txn_id" not in row or row.get("credit_debit") != "credit"
                        or row.get("amount_paise") not in possible_amounts):
                    continue
                if _plausible_bank_owner(order, row):
                    reject("NEGATIVE_EVIDENCE_CONTRADICTED")
                    break
    elif recon_rule == "none":
        if not any(row.get("status") == "captured" for row in full_captures):
            reject("NEGATIVE_EVIDENCE_CONTRADICTED")
    elif recon_rule == "net_arithmetic":
        if (not any(row.get("status") == "refund" for row in full_captures)
                or sum(row.get("amount_paise", 0) - row.get("fee_paise", 0)
                       - row.get("tax_paise", 0) for row in full_captures) != 0):
            reject("NEGATIVE_EVIDENCE_CONTRADICTED")

    if expected_config_hash is None or expected_calibration_id is None:
        reject("IDENTITY_EXPECTATION_MISSING")
    else:
        if certificate.config_hash != expected_config_hash:
            reject("CONFIG_HASH_MISMATCH")
        if certificate.calibration_id != expected_calibration_id:
            reject("CALIBRATION_ID_MISMATCH")

    return VerificationResult(valid=not errors, error_codes=tuple(errors))


def verify_certificate(
    certificate: ProofCertificate,
    source_rows: list[dict[str, object]],
    *,
    expected_config_hash: str | None = None,
    expected_calibration_id: str | None = None,
) -> VerificationResult:
    """Total public boundary: malformed untrusted inputs become error codes."""
    try:
        return _verify_certificate(
            certificate,
            source_rows,
            expected_config_hash=expected_config_hash,
            expected_calibration_id=expected_calibration_id,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return VerificationResult(valid=False, error_codes=("CERTIFICATE_SCHEMA_INVALID",))

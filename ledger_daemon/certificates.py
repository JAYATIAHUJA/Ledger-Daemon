"""Canonical, tamper-evident reconciliation proof certificates."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date
from typing import Iterable

from .source_contracts import canonical_json, sha256_hex


CERTIFICATE_VERSION = "1"
ENGINE_ID = "ledger-daemon-recon-v1"


@dataclass(frozen=True)
class AmountTerm:
    """One signed integer-paise term in the certificate's money equation."""

    name: str
    amount_paise: int
    source_row_id: str = ""
    source_field: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "amount_paise": self.amount_paise,
            "name": self.name,
            "source_field": self.source_field,
            "source_row_id": self.source_row_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AmountTerm":
        expected = {"amount_paise", "name", "source_field", "source_row_id"}
        if set(value) != expected:
            raise ValueError("invalid amount-term schema")
        if type(value["amount_paise"]) is not int:
            raise ValueError("amount term must use integer paise")
        if not all(isinstance(value[key], str) for key in expected - {"amount_paise"}):
            raise ValueError("amount-term identifiers must be strings")
        return cls(
            name=value["name"],
            amount_paise=value["amount_paise"],
            source_row_id=value["source_row_id"],
            source_field=value["source_field"],
        )


@dataclass(frozen=True)
class ProofCertificate:
    version: str
    order_id: str
    verdict: str
    source_hashes: tuple[tuple[str, str], ...]
    amount_terms: tuple[AmountTerm, ...]
    money_received_paise: int
    delta_due_paise: int
    rule_ids: tuple[str, ...]
    config_hash: str
    calibration_id: str
    generated_at: str
    proof_hash: str

    @classmethod
    def create(
        cls,
        *,
        order_id: str,
        verdict: str,
        source_hashes: dict[str, str],
        amount_terms: tuple[AmountTerm, ...],
        money_received_paise: int,
        delta_due_paise: int,
        rule_ids: tuple[str, ...],
        config_hash: str,
        calibration_id: str,
        generated_at: str,
    ) -> "ProofCertificate":
        facts = cls(
            version=CERTIFICATE_VERSION,
            order_id=order_id,
            verdict=verdict,
            source_hashes=tuple(sorted(source_hashes.items())),
            amount_terms=tuple(amount_terms),
            money_received_paise=money_received_paise,
            delta_due_paise=delta_due_paise,
            rule_ids=tuple(rule_ids),
            config_hash=config_hash,
            calibration_id=calibration_id,
            generated_at=generated_at,
            proof_hash="",
        )
        return replace(facts, proof_hash=sha256_hex(facts._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "amount_terms": [term.to_dict() for term in self.amount_terms],
            "calibration_id": self.calibration_id,
            "config_hash": self.config_hash,
            "delta_due_paise": self.delta_due_paise,
            "generated_at": self.generated_at,
            "money_received_paise": self.money_received_paise,
            "order_id": self.order_id,
            "rule_ids": list(self.rule_ids),
            "source_hashes": dict(self.source_hashes),
            "verdict": self.verdict,
            "version": self.version,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "proof_hash": self.proof_hash}

    def to_json(self) -> str:
        return canonical_json(self.to_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, encoded: str) -> "ProofCertificate":
        value = json.loads(encoded)
        if not isinstance(value, dict):
            raise ValueError("certificate must be a JSON object")
        expected = {
            "amount_terms", "calibration_id", "config_hash", "delta_due_paise",
            "generated_at", "money_received_paise", "order_id", "proof_hash",
            "rule_ids", "source_hashes", "verdict", "version",
        }
        if set(value) != expected:
            raise ValueError("invalid certificate schema")
        if not isinstance(value["source_hashes"], dict):
            raise ValueError("source_hashes must be an object")
        if not isinstance(value["amount_terms"], list) or not isinstance(value["rule_ids"], list):
            raise ValueError("amount_terms and rule_ids must be arrays")
        if any(type(value[field]) is not int
               for field in ("money_received_paise", "delta_due_paise")):
            raise ValueError("certificate money must use integer paise")
        string_fields = {
            "calibration_id", "config_hash", "generated_at", "order_id",
            "proof_hash", "verdict", "version",
        }
        if any(not isinstance(value[field], str) for field in string_fields):
            raise ValueError("certificate identifiers must be strings")
        if any(not isinstance(key, str) or not isinstance(item, str)
               for key, item in value["source_hashes"].items()):
            raise ValueError("source hashes must map string identifiers to string digests")
        if any(not isinstance(rule, str) for rule in value["rule_ids"]):
            raise ValueError("rule identifiers must be strings")
        if any(not isinstance(term, dict) for term in value["amount_terms"]):
            raise ValueError("amount terms must be objects")
        return cls(
            version=value["version"],
            order_id=value["order_id"],
            verdict=value["verdict"],
            source_hashes=tuple(sorted(value["source_hashes"].items())),
            amount_terms=tuple(AmountTerm.from_dict(term) for term in value["amount_terms"]),
            money_received_paise=value["money_received_paise"],
            delta_due_paise=value["delta_due_paise"],
            rule_ids=tuple(value["rule_ids"]),
            config_hash=value["config_hash"],
            calibration_id=value["calibration_id"],
            generated_at=value["generated_at"],
            proof_hash=value["proof_hash"],
        )


def source_rows(orders: Iterable[object], captures: Iterable[object],
                bank: Iterable[object]) -> list[dict[str, object]]:
    """Return the canonical typed rows used by both proof writer and verifier."""
    rows: list[dict[str, object]] = []
    for record in (*tuple(orders), *tuple(captures), *tuple(bank)):
        if not is_dataclass(record):
            raise TypeError("proof sources must be dataclass records")
        rows.append(asdict(record))
    return rows


def _row_id(row: dict[str, object]) -> str:
    if "payment_id" in row:
        value = row["payment_id"]
    elif "txn_id" in row:
        value = row["txn_id"]
    else:
        value = row.get("order_id")
    if not isinstance(value, str) or not value:
        raise ValueError("source row has no primary identifier")
    return value


def source_hash_map(rows: Iterable[dict[str, object]]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for row in rows:
        row_id = _row_id(row)
        if row_id in hashes:
            raise ValueError(f"duplicate source identifier {row_id!r}")
        hashes[row_id] = sha256_hex(row)
    return hashes


def batch_root(source_hashes: dict[str, str]) -> str:
    return sha256_hex({key: source_hashes[key] for key in sorted(source_hashes)})


def recon_config_hash(config: object) -> str:
    if not is_dataclass(config):
        raise TypeError("reconciliation config must be a dataclass")
    return sha256_hex({"engine_id": ENGINE_ID, "switches": asdict(config)})


def calibration_identity(q_hat: float, dataset_id: str) -> str:
    return "sha256:" + sha256_hex({
        "dataset_id": dataset_id,
        "q_hat": format(q_hat, ".17g"),
        "schema": "calibration-identity-v1",
    })


def _generated_at(rows: Iterable[dict[str, object]]) -> str:
    dates = [
        value
        for row in rows
        for field in ("due_date", "captured_at", "value_date")
        if isinstance((value := row.get(field)), str)
    ]
    watermark = max(dates, default=date(1970, 1, 1).isoformat())
    return f"{watermark}T00:00:00Z"


def _amount_terms(order: object, verdict: object,
                  evidence_rows: list[dict[str, object]]) -> tuple[AmountTerm, ...]:
    verdict_name = verdict.verdict.value
    invoice = order.amount_paise
    if verdict_name == "ambiguous":
        terms: list[AmountTerm] = []
    elif verdict_name == "genuinely_unpaid":
        terms = [
            AmountTerm("invoice", invoice, order.order_id, "amount_paise"),
            AmountTerm("unpaid_exposure", -invoice),
        ]
    elif verdict_name == "failed_not_debited":
        terms = []
    else:
        terms = [
            AmountTerm("invoice", invoice, order.order_id, "amount_paise"),
            AmountTerm("money_received", -verdict.money_received_paise),
        ]
        if verdict_name == "partially_paid":
            terms.append(AmountTerm("delta_due", -verdict.delta_due_paise))
        elif verdict_name == "paid_net_of_tds":
            terms.append(AmountTerm("tds_withheld", -(invoice - verdict.money_received_paise)))

    if verdict.evidence.pass_used in {
        "pass1_exact_utr", "pass2_amount_date", "pass3_settlement_id",
    }:
        for row in sorted((row for row in evidence_rows if "payment_id" in row),
                          key=_row_id):
            capture_name = "gateway_refund" if row.get("status") == "refund" else "gateway_capture"
            terms.extend((
                AmountTerm(capture_name, row["amount_paise"], _row_id(row), "amount_paise"),
                AmountTerm("gateway_fee", -row["fee_paise"], _row_id(row), "fee_paise"),
                AmountTerm("gateway_gst", -row["tax_paise"], _row_id(row), "tax_paise"),
            ))
        for row in sorted((row for row in evidence_rows
                           if "txn_id" in row and row.get("credit_debit") == "credit"),
                          key=_row_id):
            terms.append(AmountTerm("bank_credit", -row["amount_paise"],
                                    _row_id(row), "amount_paise"))
    elif verdict.evidence.pass_used == "pass4_fuzzy" and verdict_name != "ambiguous":
        for row in sorted((row for row in evidence_rows
                           if "txn_id" in row and row.get("credit_debit") == "credit"),
                          key=_row_id):
            terms.extend((
                AmountTerm("bank_credit", row["amount_paise"], _row_id(row), "amount_paise"),
                AmountTerm("evidence_received", -verdict.money_received_paise),
            ))
        if verdict_name == "refunded_then_repaid":
            for row in sorted((row for row in evidence_rows if "payment_id" in row),
                              key=_row_id):
                terms.extend((
                    AmountTerm(
                        "gateway_refund" if row.get("status") == "refund" else "gateway_capture",
                        row["amount_paise"], _row_id(row), "amount_paise",
                    ),
                    AmountTerm("gateway_fee", -row["fee_paise"], _row_id(row), "fee_paise"),
                    AmountTerm("gateway_gst", -row["tax_paise"], _row_id(row), "tax_paise"),
                ))
    return tuple(terms)


def _settlement_members(indexed: dict[str, dict[str, object]]) -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for row_id, row in indexed.items():
        settlement_id = row.get("settlement_id")
        if settlement_id:
            groups.setdefault(str(settlement_id), []).append(row_id)
    return {key: tuple(sorted(value)) for key, value in groups.items()}


def _build_certificate(order: object, verdict: object, source_hashes: dict[str, str],
                       config_hash: str, calibration_id: str, *,
                       indexed: dict[str, dict[str, object]], root_hash: str,
                       generated_at: str,
                       settlement_members: dict[str, tuple[str, ...]]) -> ProofCertificate:
    referenced = {order.order_id, *verdict.evidence_refs}
    if verdict.verdict.value == "refunded_then_repaid":
        referenced.update(
            row_id for row_id, row in indexed.items()
            if "payment_id" in row and row.get("order_id") == order.order_id
        )

    # A settlement bank credit can aggregate several orders. Include the complete
    # capture group so a verifier can re-sum it instead of trusting our allocation.
    settlement_ids = {
        indexed[row_id].get("settlement_id")
        for row_id in tuple(referenced)
        if row_id in indexed and indexed[row_id].get("settlement_id")
    }
    for settlement_id in settlement_ids:
        referenced.update(settlement_members.get(str(settlement_id), ()))

    missing = referenced - source_hashes.keys()
    if missing:
        raise ValueError(f"proof references unknown source rows: {sorted(missing)}")
    selected_hashes = {row_id: source_hashes[row_id] for row_id in referenced}
    selected_hashes["BATCH_ROOT"] = root_hash
    return ProofCertificate.create(
        order_id=order.order_id,
        verdict=verdict.verdict.value,
        source_hashes=selected_hashes,
        amount_terms=_amount_terms(
            order, verdict, [indexed[row_id] for row_id in referenced if row_id in indexed]
        ),
        money_received_paise=verdict.money_received_paise,
        delta_due_paise=verdict.delta_due_paise,
        rule_ids=(f"RECON.{verdict.evidence.pass_used}", f"VERDICT.{verdict.verdict.value}"),
        config_hash=config_hash,
        calibration_id=calibration_id,
        generated_at=generated_at,
    )


def build_certificate(order: object, verdict: object, source_hashes: dict[str, str],
                      config_hash: str, calibration_id: str, *,
                      rows: list[dict[str, object]]) -> ProofCertificate:
    """Build one proof; bundle callers use the precomputed linear-time path."""
    indexed = {_row_id(row): row for row in rows}
    return _build_certificate(
        order, verdict, source_hashes, config_hash, calibration_id,
        indexed=indexed,
        root_hash=batch_root(source_hashes),
        generated_at=_generated_at(rows),
        settlement_members=_settlement_members(indexed),
    )


def _safe_filename(order_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", order_id) or ".." in order_id:
        raise ValueError(f"unsafe order identifier {order_id!r}")
    return f"{order_id}.json"


def write_proof_bundle(out_dir: str, orders: list[object], captures: list[object],
                       bank: list[object], verdicts: dict[str, object], *,
                       config_hash: str, calibration_id: str) -> dict[str, object]:
    if (len(verdicts) != len(orders)
            or set(verdicts) != {order.order_id for order in orders}):
        raise ValueError("certificate count cannot differ from order count")
    rows = source_rows(orders, captures, bank)
    hashes = source_hash_map(rows)
    indexed = {_row_id(row): row for row in rows}
    root_hash = batch_root(hashes)
    generated_at = _generated_at(rows)
    members = _settlement_members(indexed)
    built: list[tuple[object, object, ProofCertificate, str]] = []
    for order in sorted(orders, key=lambda item: item.order_id):
        verdict = verdicts[order.order_id]
        certificate = _build_certificate(
            order, verdict, hashes, config_hash, calibration_id,
            indexed=indexed, root_hash=root_hash, generated_at=generated_at,
            settlement_members=members,
        )
        built.append((order, verdict, certificate, _safe_filename(order.order_id)))

    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "proof-manifest.json")
    new_files = {_safe_filename(order.order_id) for order in orders}
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            previous = json.load(fh)
    except FileNotFoundError:
        previous = {}
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("existing proof manifest is unreadable") from exc
    if not isinstance(previous, dict) or not isinstance(previous.get("certificates", {}), dict):
        raise ValueError("existing proof manifest schema is invalid")
    for old_order_id, entry in previous.get("certificates", {}).items():
        try:
            filename = _safe_filename(old_order_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("file") != filename or filename in new_files:
            continue
        stale_path = os.path.abspath(os.path.join(out_dir, filename))
        if os.path.dirname(stale_path) == os.path.abspath(out_dir):
            try:
                os.unlink(stale_path)
            except FileNotFoundError:
                pass

    entries: dict[str, dict[str, str]] = {}
    for order, verdict, certificate, filename in built:
        path = os.path.join(out_dir, filename)
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(certificate.to_json() + "\n")
        os.replace(temp_path, path)
        entries[order.order_id] = {"file": filename, "proof_hash": certificate.proof_hash}

    manifest: dict[str, object] = {
        "schema_version": "1",
        "batch_root": root_hash,
        "calibration_id": calibration_id,
        "certificate_count": len(entries),
        "certificates": entries,
        "config_hash": config_hash,
    }
    temp_manifest = manifest_path + ".tmp"
    with open(temp_manifest, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(temp_manifest, manifest_path)
    for _order, verdict, certificate, _filename in built:
        verdict.certificate_id = certificate.proof_hash
    return manifest

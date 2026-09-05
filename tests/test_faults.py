"""Faults are planned, not sprinkled (F9).

A fault plan is a function of its seed: same seed, same injections, byte for
byte. Every injected record carries the outcome the system is required to
produce, so a run can be graded instead of merely survived — an attack with no
oracle proves nothing, because whatever happened becomes the expected result.
"""

import pytest

from ledger_daemon.faults import (
    ORACLES,
    Fault,
    FaultPlan,
    Injection,
    apply_bank_faults,
    injections_json,
    plan_injections,
    tamper_certificate,
)

BANK_IDS = [f"TXN-{i:04d}" for i in range(60)]
ORDER_IDS = [f"ORD-{i:04d}" for i in range(60)]


def _plan(*faults, seed=42):
    return FaultPlan(seed=seed, faults=tuple(faults or Fault))


def test_same_seed_and_plan_produce_byte_identical_injections():
    plan = _plan()
    first = plan_injections(plan, BANK_IDS, ORDER_IDS)
    second = plan_injections(FaultPlan(seed=42, faults=plan.faults), BANK_IDS, ORDER_IDS)
    assert first == second
    assert injections_json(first) == injections_json(second)


def test_a_different_seed_moves_the_injections():
    a = plan_injections(_plan(), BANK_IDS, ORDER_IDS)
    b = plan_injections(_plan(seed=43), BANK_IDS, ORDER_IDS)
    assert injections_json(a) != injections_json(b)


def test_every_injected_record_has_a_declared_oracle():
    for injection in plan_injections(_plan(), BANK_IDS, ORDER_IDS):
        assert injection.oracle in ORACLES
        assert injection.target
        assert injection.detail


def test_every_fault_kind_is_planned_when_requested():
    planned = {i.fault for i in plan_injections(_plan(), BANK_IDS, ORDER_IDS)}
    assert planned == set(Fault)


def test_plan_hash_is_stable_and_covers_the_fault_set():
    assert _plan().plan_hash == _plan().plan_hash
    assert _plan(Fault.DROP).plan_hash != _plan(Fault.DUPLICATE).plan_hash


def test_empty_targets_plan_nothing_rather_than_raising():
    assert plan_injections(_plan(), [], []) == ()


# ---- ingestion-stage application -------------------------------------------- #

def _rows(n=20):
    return [{"txn_id": f"TXN-{i:04d}", "value_date": "2026-01-05",
             "amount_paise": 100000 + i, "credit_debit": "credit",
             "utr": f"HDFCP1234{i:04d}", "narration": f"NEFT-HDFCP1234{i:04d}-ACME-INV{i:04d}",
             "balance_after": 0}
            for i in range(n)]


def test_duplicate_fault_repeats_a_row_verbatim():
    rows = _rows()
    injection = Injection(Fault.DUPLICATE, "TXN-0003", "duplicate delivery",
                          ORACLES["quarantined"])
    out = apply_bank_faults(rows, (injection,))
    assert len(out) == len(rows) + 1
    assert [r for r in out if r["txn_id"] == "TXN-0003"] == [rows[3], rows[3]]


def test_drop_fault_removes_the_row():
    rows = _rows()
    injection = Injection(Fault.DROP, "TXN-0005", "feed gap", ORACLES["no_wrong_chase"])
    out = apply_bank_faults(rows, (injection,))
    assert all(r["txn_id"] != "TXN-0005" for r in out)
    assert len(out) == len(rows) - 1


def test_reorder_keeps_the_multiset_of_rows():
    rows = _rows()
    injection = Injection(Fault.REORDER, "TXN-0000", "out-of-order feed",
                          ORACLES["unchanged"])
    out = apply_bank_faults(rows, (injection,))
    assert sorted(r["txn_id"] for r in out) == sorted(r["txn_id"] for r in rows)
    assert [r["txn_id"] for r in out] != [r["txn_id"] for r in rows]


def test_truncate_shortens_the_narration_only():
    rows = _rows()
    injection = Injection(Fault.TRUNCATE, "TXN-0007", "field width", ORACLES["no_wrong_chase"])
    out = apply_bank_faults(rows, (injection,))
    hit = next(r for r in out if r["txn_id"] == "TXN-0007")
    assert len(hit["narration"]) < len(rows[7]["narration"])
    assert hit["amount_paise"] == rows[7]["amount_paise"]


def test_malform_json_breaks_the_money_field_so_validation_fails_closed():
    rows = _rows()
    injection = Injection(Fault.MALFORM_JSON, "TXN-0009", "bad export",
                          ORACLES["quarantined"])
    out = apply_bank_faults(rows, (injection,))
    hit = next(r for r in out if r["txn_id"] == "TXN-0009")
    assert type(hit["amount_paise"]) is not int


def test_prompt_injection_writes_instruction_text_into_the_narration():
    rows = _rows()
    injection = Injection(Fault.PROMPT_INJECTION, "TXN-0011", "hostile narration",
                          ORACLES["no_authority_granted"])
    out = apply_bank_faults(rows, (injection,))
    hit = next(r for r in out if r["txn_id"] == "TXN-0011")
    assert "IGNORE" in hit["narration"].upper()
    assert hit["amount_paise"] == rows[11]["amount_paise"]


def test_applying_the_same_injections_twice_gives_the_same_rows():
    rows = _rows()
    plan = _plan()
    injections = tuple(i for i in plan_injections(plan, [r["txn_id"] for r in rows], ORDER_IDS)
                       if i.fault in (Fault.DUPLICATE, Fault.DROP, Fault.REORDER,
                                      Fault.TRUNCATE, Fault.MALFORM_JSON,
                                      Fault.PROMPT_INJECTION))
    assert apply_bank_faults(rows, injections) == apply_bank_faults(rows, injections)


def test_bank_faults_do_not_mutate_the_caller_rows():
    rows = _rows()
    before = [dict(r) for r in rows]
    apply_bank_faults(rows, (Injection(Fault.TRUNCATE, "TXN-0002", "x",
                                       ORACLES["no_wrong_chase"]),))
    assert rows == before


# ---- proof-stage application ------------------------------------------------ #

def test_hash_tamper_changes_exactly_one_field_and_leaves_valid_json():
    certificate = {"order_id": "ORD-1", "verdict": "genuinely_unpaid",
                   "money_received_paise": 0, "proof_hash": "a" * 64}
    tampered = tamper_certificate(certificate)
    assert tampered != certificate
    assert tampered["order_id"] == "ORD-1"
    assert tampered["money_received_paise"] != certificate["money_received_paise"]


def test_hash_tamper_is_deterministic():
    certificate = {"order_id": "ORD-1", "money_received_paise": 500}
    assert tamper_certificate(certificate) == tamper_certificate(certificate)


@pytest.mark.parametrize("fault", list(Fault))
def test_every_fault_declares_which_stage_it_attacks(fault):
    assert fault.stage in ("ingestion", "case_transition", "executor", "proof")

"""The agent-facing case surface must refuse stale writes as firmly as the UI does.

An autonomous caller is exactly the consumer most likely to act on a view it
read a while ago, so case_transition demands the version it last saw and
returns a structured refusal instead of a state change.
"""

import pytest

from ledger_daemon.cases import CaseState
from ledger_daemon.mcp_server import Service


@pytest.fixture
def svc(tmp_path):
    return Service(str(tmp_path))


def test_the_tool_name_is_not_shadowed_by_the_store(svc):
    assert callable(svc.cases), "cases() must stay the tool; the store lives elsewhere"


def test_cases_lists_state_version_and_history(svc):
    case = svc.case_store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    rows = svc.cases()
    assert [r["case_id"] for r in rows] == [case.case_id]
    assert rows[0]["state"] == "OPEN" and rows[0]["version"] == 1
    assert [h["to"] for h in rows[0]["history"]] == ["OPEN"]


def test_a_stale_version_is_refused_with_a_named_error(svc):
    case = svc.case_store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    ok = svc.case_transition(case.case_id, 1, "ASSIGNED", "agent-1")
    assert ok["state"] == "ASSIGNED" and ok["version"] == 2

    stale = svc.case_transition(case.case_id, 1, "INVESTIGATING", "agent-2")
    assert stale["error_type"] == "VersionConflict"
    assert svc.case_store.get(case.case_id).state is CaseState.ASSIGNED


def test_an_undeclared_edge_is_refused(svc):
    case = svc.case_store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    refused = svc.case_transition(case.case_id, 1, "RESOLVED", "agent-1")
    assert refused["error_type"] == "IllegalTransition"
    assert svc.case_store.get(case.case_id).version == 1


def test_a_state_outside_the_taxonomy_is_refused(svc):
    case = svc.case_store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    refused = svc.case_transition(case.case_id, 1, "FIXED_IT_TRUST_ME", "agent-1")
    assert "unknown case state" in refused["error"]
    assert refused["states"] == [s.value for s in CaseState]


def test_transitions_record_the_agent_that_made_them(svc):
    case = svc.case_store.open_case("ORD-1", "PROOF_UNVERIFIED", "proof-1")
    svc.case_transition(case.case_id, 1, "ASSIGNED", "agent-1",
                        evidence_refs=["TXN-9"])
    event = svc.case_store.events(case.case_id)[-1]
    assert event.actor == "agent-1" and event.evidence_refs == ("TXN-9",)


def test_mcp_explain_renders_the_same_proof_as_the_cli(tmp_path):
    """One proof hash, whichever surface the caller looks at."""
    from ledger_daemon.certificates import (
        batch_root, calibration_identity, recon_config_hash, source_hash_map,
        source_rows, write_proof_bundle,
    )
    from ledger_daemon.cli import render_proof
    from ledger_daemon.datagen import generate, load_batch
    from ledger_daemon.recon import FULL, reconcile

    root = tmp_path / "root"
    batch = root / "data" / "batch"
    generate(11, 60, str(batch))
    orders, captures, bank, _truth = load_batch(str(batch))
    result = reconcile(orders, captures, bank, q_hat=0.001)
    rows = source_rows(orders, captures, bank)
    write_proof_bundle(
        str(root / "proofs"), orders, captures, bank, result.verdicts,
        config_hash=recon_config_hash(FULL),
        calibration_id=calibration_identity(0.001, batch_root(source_hash_map(rows))),
    )

    svc = Service(str(root))
    order_id = sorted(result.verdicts)[0]
    text = svc.explain(order_id)
    rendered = render_proof(str(root / "proofs"), order_id)

    assert rendered in text
    assert "no issued proof" not in rendered

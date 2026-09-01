from uuid import uuid4

import pytest

from ci_agent.playbook import PlaybookReplay, ResearchPlaybookStore


def test_playbook_candidate_replay_gate_and_promotion(tmp_path) -> None:
    store = ResearchPlaybookStore(tmp_path)
    tenant = "tenant-a"
    run_id = uuid4()
    candidate = store.propose(tenant, run_id, "比较企业版价格、AI 功能和安全能力", "investigation")
    assert "price" in candidate.playbook.dimensions
    store.record_replay(PlaybookReplay(replay_id="r1", tenant_id=tenant, run_id=run_id, brief="a", selected_mode="investigation", report_title="a", contract_passed=True))
    store.record_replay(PlaybookReplay(replay_id="r2", tenant_id=tenant, run_id=uuid4(), brief="b", selected_mode="verify", report_title="b", contract_passed=True))
    evaluation = store.promote(candidate.candidate_id, tenant)
    assert evaluation.eligible_for_promotion
    assert store.active(tenant).version == candidate.playbook.version


def test_playbook_promotion_is_tenant_scoped(tmp_path) -> None:
    store = ResearchPlaybookStore(tmp_path)
    candidate = store.propose("tenant-a", uuid4(), "价格", "investigation")
    with pytest.raises(FileNotFoundError):
        store.promote(candidate.candidate_id, "tenant-b")

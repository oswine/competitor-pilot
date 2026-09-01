from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from ci_agent.evidence import EvidenceLedger
from ci_agent.models import Claim, Evidence, ResearchBundle, SourceType, VerifiedFinding
from ci_agent.orchestrator import ResearchOrchestrator


def evidence() -> Evidence:
    return Evidence(
        id=uuid4(),
        source_url="https://example.com/pricing",
        publisher="Example",
        retrieved_at=datetime.now(timezone.utc),
        content_hash="sha256:" + "0" * 64,
        excerpt="Enterprise price is 20 USD per user.",
        locator="body",
        source_type=SourceType.PRICING,
        credibility=0.9,
    )


def test_bundle_cannot_reference_evidence_from_another_run(tmp_path) -> None:
    allowed = evidence()
    foreign = evidence()
    orchestrator = ResearchOrchestrator.__new__(ResearchOrchestrator)
    orchestrator.ledger = EvidenceLedger(tmp_path)
    orchestrator.ledger.append(allowed)
    orchestrator.ledger.append(foreign)
    claim = Claim(subject="Example", predicate="price", value="20 USD", evidence_ids=[foreign.id], confidence=0.9, fact_or_inference="fact")
    bundle = ResearchBundle(
        claims=[claim],
        finding=VerifiedFinding(finding_id="price", statement="Example costs 20 USD.", claim_ids=[claim.id], evidence_ids=[foreign.id], fact_or_inference="fact", confidence=0.9),
    )
    with pytest.raises(ValueError, match="outside this tenant run"):
        orchestrator._verify_bundle_scope(bundle, [allowed])


def test_claim_confidence_cannot_exceed_source(tmp_path) -> None:
    item = evidence().model_copy(update={"credibility": 0.75})
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    claim = Claim(
        subject="Example",
        predicate="price",
        value="20 USD",
        evidence_ids=[item.id],
        confidence=0.9,
        fact_or_inference="fact",
    )
    with pytest.raises(ValueError, match="confidence exceeds"):
        ledger.verify_claim(claim)


def test_named_product_claim_cannot_use_another_product_source(tmp_path) -> None:
    notion = evidence().model_copy(
        update={"publisher": "Notion", "excerpt": "Notion Free $0 per member / month"}
    )
    coda = evidence().model_copy(
        update={"publisher": "ComparEdge", "excerpt": '{"name":"Coda","price":0}'}
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(notion)
    ledger.append(coda)
    claim = Claim(
        subject="Notion",
        predicate="public_pricing",
        value="0",
        evidence_ids=[coda.id],
        confidence=0.7,
        fact_or_inference="fact",
    )
    with pytest.raises(ValueError, match="does not match cited source product"):
        ledger.verify_claim(claim)


def test_finding_must_cover_every_claim(tmp_path) -> None:
    item = evidence()
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    first = Claim(
        subject="Example", predicate="price", value="20 USD", evidence_ids=[item.id], confidence=0.9, fact_or_inference="fact"
    )
    second = Claim(
        subject="Example", predicate="unit", value="user", evidence_ids=[item.id], confidence=0.9, fact_or_inference="inference"
    )
    finding = VerifiedFinding(
        finding_id="coverage",
        statement="Example has a verified price and billing unit.",
        claim_ids=[first.id],
        evidence_ids=[item.id],
        fact_or_inference="inference",
        confidence=0.9,
    )
    with pytest.raises(ValueError, match="claim coverage mismatch"):
        ledger.verify_finding(finding, [first, second])


def test_near_miss_evidence_uuid_is_repaired_only_when_unique(tmp_path) -> None:
    item = evidence()
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    raw = str(item.id)
    # Swap two adjacent hex characters while retaining a valid UUID.
    swapped = raw[:14] + raw[15] + raw[14] + raw[16:]
    typo = UUID(swapped)
    claim = Claim(
        subject="Example", predicate="price", value="20 USD", evidence_ids=[typo], confidence=0.9, fact_or_inference="fact"
    )
    finding = VerifiedFinding(
        finding_id="repair",
        statement="Example has a verified price from the source.",
        claim_ids=[claim.id],
        evidence_ids=[typo],
        fact_or_inference="fact",
        confidence=0.9,
    )
    repaired, repairs = ledger.repair_bundle_evidence_ids(ResearchBundle(claims=[claim], finding=finding), [item])
    assert repairs == [(swapped, raw)]
    assert repaired.claims[0].evidence_ids == [item.id]
    assert repaired.finding.evidence_ids == [item.id]


def test_placeholder_uuid_is_repaired_from_claim_content(tmp_path) -> None:
    item = evidence().model_copy(
        update={"publisher": "ClickUp", "excerpt": "ClickUp is ISO 27001 Certified."}
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    placeholder = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    claim = Claim(
        subject="ClickUp security",
        predicate="certification",
        value="ISO 27001 Certified",
        evidence_ids=[placeholder],
        confidence=0.9,
        fact_or_inference="fact",
    )
    finding = VerifiedFinding(
        finding_id="placeholder",
        statement="ClickUp states ISO 27001 Certified; Next action: verify the certificate.",
        claim_ids=[claim.id],
        evidence_ids=[placeholder],
        fact_or_inference="inference",
        confidence=0.9,
    )
    repaired, repairs = ledger.repair_bundle_evidence_ids(
        ResearchBundle(claims=[claim], finding=finding), [item]
    )
    assert repaired.claims[0].evidence_ids == [item.id]
    assert repaired.finding.evidence_ids == [item.id]
    assert any(original == str(placeholder) for original, _ in repairs)


def test_finding_numbers_must_exist_in_atomic_claim_values(tmp_path) -> None:
    item = evidence()
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    claim = Claim(
        subject="Example", predicate="price", value="20 USD", evidence_ids=[item.id], confidence=0.9, fact_or_inference="fact"
    )
    finding = VerifiedFinding(
        finding_id="number-grounding",
        statement="Example costs 20 USD, not the unsupported 30 USD.",
        claim_ids=[claim.id],
        evidence_ids=[item.id],
        fact_or_inference="inference",
        confidence=0.9,
    )
    with pytest.raises(ValueError, match="numbers without atomic claim"):
        ledger.verify_finding(finding, [claim])


def test_uuid_fragments_are_not_material_numbers() -> None:
    values = EvidenceLedger._material_numbers(
        "evidence 027910c3-cf2a-5f39-8e51-220f54e8a2d7 supports $20 in 2026"
    )
    assert values == {"20", "2026"}


def test_evidence_uuid_short_prefix_is_removed_before_number_checks() -> None:
    evidence_id = UUID("69304127-ba87-5120-847f-0738bf12d1b7")
    cleaned = EvidenceLedger._strip_evidence_identifiers(
        "Source 69304127 supports $20.", [evidence_id]
    )
    assert EvidenceLedger._material_numbers(cleaned) == {"20"}


def test_missing_finding_number_is_grounded_to_matching_product(tmp_path) -> None:
    notion = evidence().model_copy(
        update={"publisher": "Notion", "excerpt": "Notion Plus costs $12 per member.", "credibility": 0.82}
    )
    coda = evidence().model_copy(
        update={"publisher": "Coda", "excerpt": "Coda Pro costs $12 per Doc Maker.", "credibility": 0.95}
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(notion)
    ledger.append(coda)
    base = Claim(
        subject="Coda", predicate="billing unit", value="Doc Maker", evidence_ids=[coda.id], confidence=0.9, fact_or_inference="fact"
    )
    finding = VerifiedFinding(
        finding_id="numeric-repair",
        statement="Coda Pro costs $12 per Doc Maker according to the official source.",
        claim_ids=[base.id],
        evidence_ids=[coda.id],
        fact_or_inference="inference",
        confidence=0.9,
    )
    repaired, repairs = ledger.ground_missing_finding_numbers(
        ResearchBundle(claims=[base], finding=finding), [notion, coda], ["Notion", "Coda"]
    )
    assert repairs[0]["publisher"] == "Coda"
    assert repaired.claims[-1].value == "$12"
    assert repaired.claims[-1].evidence_ids == [coda.id]
    ledger.verify_finding(repaired.finding, repaired.claims)


def test_shared_number_can_be_grounded_to_multiple_equal_primary_sources(tmp_path) -> None:
    notion = evidence().model_copy(
        update={"publisher": "Notion", "excerpt": "Notion Free costs $0.", "credibility": 0.95}
    )
    coda = evidence().model_copy(
        update={"publisher": "Coda", "excerpt": "Coda Free costs $0.", "credibility": 0.95}
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(notion)
    ledger.append(coda)
    base = Claim(
        subject="Notion", predicate="plan", value="Free", evidence_ids=[notion.id], confidence=0.9, fact_or_inference="fact"
    )
    finding = VerifiedFinding(
        finding_id="shared-number",
        statement="Notion and Coda both list Free at $0; Next action: verify current terms.",
        claim_ids=[base.id],
        evidence_ids=[notion.id, coda.id],
        fact_or_inference="inference",
        confidence=0.9,
    )
    repaired, repairs = ledger.ground_missing_finding_numbers(
        ResearchBundle(claims=[base], finding=finding), [notion, coda], ["Notion", "Coda"]
    )
    assert {repair["publisher"] for repair in repairs} == {"Notion", "Coda"}
    assert len(repaired.claims) == 3


def test_secondary_source_claim_is_explicitly_qualified(tmp_path) -> None:
    item = evidence().model_copy(
        update={"publisher": "ComparEdge", "excerpt": '{"name":"Notion","price":12}', "credibility": 0.82}
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    claim = Claim(
        subject="Notion Plus", predicate="price", value="12", evidence_ids=[item.id], confidence=0.82, fact_or_inference="fact"
    )
    finding = VerifiedFinding(
        finding_id="secondary",
        statement="Notion has a secondary-source price record; Next action: verify the official price.",
        claim_ids=[claim.id],
        evidence_ids=[item.id],
        fact_or_inference="inference",
        confidence=0.8,
    )
    repaired, repairs = ledger.qualify_secondary_source_claims(ResearchBundle(claims=[claim], finding=finding))
    assert repairs[0]["publishers"] == "ComparEdge"
    assert repaired.claims[0].subject.startswith("ComparEdge record about")
    assert repaired.claims[0].predicate.startswith("reports ")


def test_bundle_confidence_is_capped_to_source(tmp_path) -> None:
    item = evidence().model_copy(update={"credibility": 0.72})
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    claim = Claim(
        subject="Example", predicate="price", value="20 USD", evidence_ids=[item.id], confidence=0.95, fact_or_inference="fact"
    )
    finding = VerifiedFinding(
        finding_id="confidence",
        statement="Example costs 20 USD; Next action: verify before purchase.",
        claim_ids=[claim.id],
        evidence_ids=[item.id],
        fact_or_inference="inference",
        confidence=0.9,
    )
    repaired, repairs = ledger.cap_bundle_confidence(ResearchBundle(claims=[claim], finding=finding))
    assert repaired.claims[0].confidence == 0.72
    assert repaired.finding.confidence == 0.72
    assert {repair["kind"] for repair in repairs} == {"claim", "finding"}


def test_supported_nonverbatim_fact_is_downgraded_to_inference(tmp_path) -> None:
    item = evidence().model_copy(
        update={"publisher": "Coda", "excerpt": "Coda Pro costs $12 per Doc Maker, billed annually."}
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    claim = Claim(
        subject="Coda Pro",
        predicate="price summary",
        value="$12 per Doc Maker annually",
        evidence_ids=[item.id],
        confidence=0.9,
        fact_or_inference="fact",
    )
    finding = VerifiedFinding(
        finding_id="paraphrase",
        statement="Coda Pro costs $12 per Doc Maker; Next action: verify the current quote.",
        claim_ids=[claim.id],
        evidence_ids=[item.id],
        fact_or_inference="inference",
        confidence=0.9,
    )
    repaired, repairs = ledger.downgrade_unverbatim_fact_claims(ResearchBundle(claims=[claim], finding=finding))
    assert repairs
    assert repaired.claims[0].fact_or_inference == "inference"


def test_finding_claim_ids_are_repaired_to_returned_claims(tmp_path) -> None:
    item = evidence()
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    claim = Claim(
        subject="Example",
        predicate="price",
        value="20 USD",
        evidence_ids=[item.id],
        confidence=0.9,
        fact_or_inference="fact",
    )
    finding = VerifiedFinding(
        finding_id="claim-id-repair",
        statement="Example costs 20 USD. Next action: confirm the quote.",
        claim_ids=[UUID("00000000-0000-0000-0000-000000000000")],
        evidence_ids=[item.id],
        fact_or_inference="inference",
        confidence=0.9,
    )
    repaired, changed = ledger.repair_finding_claim_ids(ResearchBundle(claims=[claim], finding=finding))
    assert changed is True
    assert repaired.finding.claim_ids == [claim.id]


def test_claim_url_must_be_present_in_its_cited_evidence(tmp_path) -> None:
    item = evidence().model_copy(update={"excerpt": "A pricing page without outbound URLs."})
    ledger = EvidenceLedger(tmp_path)
    ledger.append(item)
    claim = Claim(
        subject="Example",
        predicate="next action",
        value="https://example.com/next-action",
        evidence_ids=[item.id],
        confidence=0.8,
        fact_or_inference="inference",
    )
    with pytest.raises(ValueError, match="URL not present"):
        ledger.verify_claim(claim)

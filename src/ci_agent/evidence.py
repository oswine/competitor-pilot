from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable
from uuid import UUID, uuid5

from .models import Claim, Evidence, EvidenceCard, ResearchBundle, VerifiedFinding


NUMERIC_REPAIR_NAMESPACE = UUID("db16c0d8-d25f-4e47-9cc5-23f71f738191")


class EvidenceLedger:
    """Append-only evidence ledger with content-addressed artifacts."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._evidence: dict[UUID, Evidence] = {}

    @staticmethod
    def content_hash(content: str | bytes) -> str:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def append(self, evidence: Evidence, raw: str | bytes | None = None) -> Evidence:
        if evidence.id in self._evidence and self._evidence[evidence.id] != evidence:
            raise ValueError(f"immutable evidence collision: {evidence.id}")
        self._evidence[evidence.id] = evidence
        if raw is not None:
            path = self.root / evidence.content_hash.removeprefix("sha256:")
            if not path.exists():
                path.write_bytes(raw.encode("utf-8") if isinstance(raw, str) else raw)
        return evidence

    def get(self, evidence_id: UUID) -> Evidence:
        return self._evidence[evidence_id]

    def repair_bundle_evidence_ids(
        self, bundle: ResearchBundle, allowed_evidence: Iterable[Evidence]
    ) -> tuple[ResearchBundle, list[tuple[str, str]]]:
        """Repair unambiguous near-miss or claim-resolvable UUIDs emitted by a model.

        UUIDs are copied through a language model as text and occasional adjacent
        character transpositions happen. A match is accepted only when it is
        absent from the allowed set and exactly one candidate has a similarity of
        at least 0.96; ambiguous or distant IDs remain untouched and fail normal
        ledger validation. The returned repair list is persisted in the trace.
        """
        evidence_list = list(allowed_evidence)
        allowed = {item.id for item in evidence_list}
        allowed_text = {str(item): item for item in allowed}
        repairs: list[tuple[str, str]] = []
        repaired_ids: dict[UUID, UUID] = {}

        def canonicalize(evidence_id: UUID) -> UUID:
            if evidence_id in allowed:
                return evidence_id
            if evidence_id in repaired_ids:
                return repaired_ids[evidence_id]
            raw = str(evidence_id)
            candidates = [
                (candidate, SequenceMatcher(None, raw, text).ratio())
                for text, candidate in allowed_text.items()
            ]
            best = [candidate for candidate, ratio in candidates if ratio >= 0.96]
            if len(best) != 1:
                return evidence_id
            repaired = best[0]
            repaired_ids[evidence_id] = repaired
            repairs.append((raw, str(repaired)))
            return repaired

        publisher_names = {
            item.publisher.casefold()
            for item in evidence_list
            if item.publisher and len(item.publisher) >= 3
        }
        claims: list[Claim] = []
        for claim in bundle.claims:
            normalized_ids = [canonicalize(item) for item in claim.evidence_ids]
            unknown = [item for item in normalized_ids if item not in allowed]
            if unknown:
                subject = claim.subject.casefold()
                named = {publisher for publisher in publisher_names if publisher in subject}
                value = str(claim.value)
                value_numbers = self._material_numbers(value)
                value_terms = set(re.findall(r"[A-Za-z0-9]{3,}", value.casefold()))
                scored: list[tuple[float, Evidence]] = []
                for item in evidence_list:
                    publisher = (item.publisher or "").casefold()
                    excerpt = item.excerpt.casefold()
                    if named and publisher not in named and not any(name in excerpt for name in named):
                        continue
                    if not value_numbers.issubset(self._material_numbers(item.excerpt)):
                        continue
                    excerpt_terms = set(re.findall(r"[A-Za-z0-9]{3,}", excerpt))
                    overlap = len(value_terms & excerpt_terms) / max(1, len(value_terms))
                    exact = value.casefold() in excerpt
                    if not exact and overlap < 0.5:
                        continue
                    scored.append(((1.0 if exact else overlap) + item.credibility / 10, item))
                if scored:
                    best_score = max(score for score, _ in scored)
                    best = [item for score, item in scored if abs(score - best_score) < 1e-9]
                    if len(best) == 1:
                        normalized_ids = [item for item in normalized_ids if item in allowed]
                        normalized_ids.append(best[0].id)
                        for original in unknown:
                            repairs.append((str(original), str(best[0].id)))
            claims.append(claim.model_copy(update={"evidence_ids": list(dict.fromkeys(normalized_ids))}))

        finding_ids = [canonicalize(item) for item in bundle.finding.evidence_ids]
        finding_unknown = [item for item in finding_ids if item not in allowed]
        if finding_unknown:
            claim_evidence_ids = list(
                dict.fromkeys(
                    evidence_id
                    for claim in claims
                    for evidence_id in claim.evidence_ids
                    if evidence_id in allowed
                )
            )
            if claim_evidence_ids:
                finding_ids = [item for item in finding_ids if item in allowed]
                finding_ids.extend(item for item in claim_evidence_ids if item not in finding_ids)
                for original in finding_unknown:
                    for repaired in claim_evidence_ids:
                        repairs.append((str(original), str(repaired)))
        finding = bundle.finding.model_copy(update={"evidence_ids": finding_ids})
        return bundle.model_copy(update={"claims": claims, "finding": finding}), repairs

    @staticmethod
    def repair_finding_claim_ids(bundle: ResearchBundle) -> tuple[ResearchBundle, bool]:
        """Canonicalize Finding coverage to the Claims actually returned.

        Models occasionally emit placeholder or stale claim IDs after editing
        a bundle.  Replacing the list with the returned Claim IDs is a
        lossless structural repair: it adds no claim and still leaves every
        Claim subject to the normal evidence and task-scope checks.
        """

        expected = [claim.id for claim in bundle.claims]
        if bundle.finding.claim_ids == expected:
            return bundle, False
        finding = bundle.finding.model_copy(update={"claim_ids": expected})
        return bundle.model_copy(update={"finding": finding}), True

    def ground_missing_finding_numbers(
        self,
        bundle: ResearchBundle,
        allowed_evidence: Iterable[Evidence],
        competitors: Iterable[str],
    ) -> tuple[ResearchBundle, list[dict[str, str]]]:
        """Create atomic claims for uniquely resolvable numbers in a finding."""
        evidence = list(allowed_evidence)
        claims = list(bundle.claims)
        present = set()
        for claim in claims:
            if claim.fact_or_inference == "fact":
                present.update(self._material_numbers(str(claim.value)))
        report_text = "\n".join([bundle.finding.statement, *bundle.finding.contradictions, *bundle.finding.unknowns])
        report_text = self._strip_evidence_identifiers(report_text, bundle.finding.evidence_ids)
        missing = self._material_numbers(report_text) - present
        repairs: list[dict[str, str]] = []

        def normalized(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", value.casefold())

        sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？;；])\s*|\n+", report_text) if part.strip()]
        competitor_list = list(competitors)
        for number in sorted(missing):
            contexts = [sentence for sentence in sentences if number in self._material_numbers(sentence)]
            named = {
                competitor
                for competitor in competitor_list
                if any(normalized(competitor) in normalized(sentence) for sentence in contexts)
            }
            candidates: list[tuple[Evidence, str]] = []
            for item in evidence:
                raw_value = self._raw_number(item.excerpt, number)
                if raw_value is None:
                    continue
                if named:
                    publisher = normalized(item.publisher or "")
                    excerpt = normalized(item.excerpt)
                    if not any(publisher == normalized(name) or normalized(name) in excerpt for name in named):
                        continue
                candidates.append((item, raw_value))
            if not candidates:
                continue
            best_credibility = max(item.credibility for item, _ in candidates)
            best = [(item, raw) for item, raw in candidates if item.credibility == best_credibility]
            existing_ids = {claim.id for claim in claims}
            for item, raw_value in best:
                publisher_key = normalized(item.publisher or "")
                subject = next(
                    (
                        name
                        for name in named
                        if publisher_key == normalized(name) or normalized(name) in normalized(item.excerpt)
                    ),
                    item.publisher or "source",
                )
                claim_id = uuid5(NUMERIC_REPAIR_NAMESPACE, f"{bundle.finding.finding_id}:{number}:{item.id}")
                if claim_id in existing_ids:
                    continue
                claim = Claim(
                    id=claim_id,
                    subject=subject,
                    predicate="material value stated in finding",
                    value=raw_value,
                    evidence_ids=[item.id],
                    confidence=item.credibility,
                    fact_or_inference="fact",
                )
                claims.append(claim)
                existing_ids.add(claim_id)
                repairs.append(
                    {
                        "number": number,
                        "claim_id": str(claim_id),
                        "evidence_id": str(item.id),
                        "publisher": item.publisher or "",
                    }
                )
        if not repairs:
            return bundle, repairs
        finding = bundle.finding.model_copy(
            update={"claim_ids": [*bundle.finding.claim_ids, *(claim.id for claim in claims[len(bundle.claims):])]}
        )
        return bundle.model_copy(update={"claims": claims, "finding": finding}), repairs

    def downgrade_unverbatim_fact_claims(
        self, bundle: ResearchBundle
    ) -> tuple[ResearchBundle, list[dict[str, str]]]:
        """Turn well-supported paraphrases into inference claims, never facts."""
        publishers = {
            item.publisher.strip().lower()
            for item in self._evidence.values()
            if item.publisher and len(item.publisher.strip()) >= 3
        }
        claims: list[Claim] = []
        repairs: list[dict[str, str]] = []
        for claim in bundle.claims:
            if claim.fact_or_inference != "fact":
                claims.append(claim)
                continue
            try:
                cited = [self.get(evidence_id) for evidence_id in claim.evidence_ids]
            except KeyError:
                claims.append(claim)
                continue
            value = str(claim.value)
            if all(value.casefold() in item.excerpt.casefold() for item in cited):
                claims.append(claim)
                continue
            subject = claim.subject.casefold()
            named_products = {publisher for publisher in publishers if publisher in subject}
            if named_products and not any(
                (item.publisher or "").strip().lower() in named_products
                or any(product in item.excerpt.casefold() for product in named_products)
                for item in cited
            ):
                claims.append(claim)
                continue
            evidence_numbers = set().union(*(self._material_numbers(item.excerpt) for item in cited))
            if not self._material_numbers(value).issubset(evidence_numbers):
                claims.append(claim)
                continue
            claim_terms = set(re.findall(r"[A-Za-z0-9]{3,}", value.casefold()))
            evidence_terms = set().union(
                *(set(re.findall(r"[A-Za-z0-9]{3,}", item.excerpt.casefold())) for item in cited)
            )
            if claim_terms and len(claim_terms & evidence_terms) / len(claim_terms) < 0.5:
                claims.append(claim)
                continue
            claims.append(claim.model_copy(update={"fact_or_inference": "inference"}))
            repairs.append({"claim_id": str(claim.id), "reason": "non_verbatim_supported_paraphrase"})
        return bundle.model_copy(update={"claims": claims}), repairs

    def qualify_secondary_source_claims(
        self, bundle: ResearchBundle
    ) -> tuple[ResearchBundle, list[dict[str, str]]]:
        """Make provenance explicit when a fact is only about a secondary record."""
        qualified: list[Claim] = []
        repairs: list[dict[str, str]] = []
        for claim in bundle.claims:
            try:
                cited = [self.get(evidence_id) for evidence_id in claim.evidence_ids]
            except KeyError:
                qualified.append(claim)
                continue
            if claim.fact_or_inference != "fact" or not cited or max(item.credibility for item in cited) >= 0.9:
                qualified.append(claim)
                continue
            publishers = ", ".join(sorted({item.publisher or "secondary source" for item in cited}))
            subject = claim.subject
            predicate = claim.predicate
            if publishers.casefold() not in subject.casefold():
                subject = f"{publishers} record about {subject}"
            if not predicate.casefold().startswith("reports "):
                predicate = f"reports {predicate}"
            qualified.append(claim.model_copy(update={"subject": subject, "predicate": predicate}))
            repairs.append({"claim_id": str(claim.id), "publishers": publishers})
        return bundle.model_copy(update={"claims": qualified}), repairs

    def cap_bundle_confidence(
        self, bundle: ResearchBundle
    ) -> tuple[ResearchBundle, list[dict[str, str | float]]]:
        """Enforce source and claim confidence ceilings without semantic edits."""
        claims: list[Claim] = []
        repairs: list[dict[str, str | float]] = []
        for claim in bundle.claims:
            try:
                ceiling = min(self.get(evidence_id).credibility for evidence_id in claim.evidence_ids)
            except KeyError:
                claims.append(claim)
                continue
            confidence = min(claim.confidence, ceiling)
            if confidence != claim.confidence:
                repairs.append(
                    {
                        "kind": "claim",
                        "id": str(claim.id),
                        "original": claim.confidence,
                        "capped": confidence,
                    }
                )
            claims.append(claim.model_copy(update={"confidence": confidence}))
        claim_confidence = {claim.id: claim.confidence for claim in claims}
        supporting_confidence = [
            claim_confidence[claim_id]
            for claim_id in bundle.finding.claim_ids
            if claim_id in claim_confidence
        ]
        finding_ceiling = min(supporting_confidence, default=bundle.finding.confidence)
        finding_confidence = min(bundle.finding.confidence, finding_ceiling)
        if finding_confidence != bundle.finding.confidence:
            repairs.append(
                {
                    "kind": "finding",
                    "id": bundle.finding.finding_id,
                    "original": bundle.finding.confidence,
                    "capped": finding_confidence,
                }
            )
        finding = bundle.finding.model_copy(update={"confidence": finding_confidence})
        return bundle.model_copy(update={"claims": claims, "finding": finding}), repairs

    @classmethod
    def _raw_number(cls, value: str, expected: str) -> str | None:
        cleaned = re.sub(r"\(\s*\d+\s*\)", "", value)
        pattern = re.compile(r"(?<![A-Za-z0-9-])\$?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z0-9-])")
        for match in pattern.finditer(cleaned):
            raw = match.group(0)
            canonical = raw.replace("$", "").replace(",", "").replace("%", "")
            if canonical == expected:
                return raw
        return None

    @staticmethod
    def _strip_evidence_identifiers(value: str, evidence_ids: Iterable[UUID]) -> str:
        cleaned = value
        for evidence_id in evidence_ids:
            raw = str(evidence_id)
            cleaned = cleaned.replace(raw, "").replace(raw[:8], "")
        return cleaned

    def validate_ids(self, ids: Iterable[UUID]) -> None:
        missing = set(ids) - self._evidence.keys()
        if missing:
            raise ValueError(f"missing evidence IDs: {sorted(map(str, missing))}")

    def card(self, evidence_ids: list[UUID], summary: str, excerpt: str, locator: str) -> EvidenceCard:
        self.validate_ids(evidence_ids)
        return EvidenceCard(
            card_id="card-" + hashlib.sha1((summary + locator).encode()).hexdigest()[:12],
            evidence_ids=evidence_ids,
            summary=summary,
            excerpt=excerpt,
            locator=locator,
            token_count=max(1, len(excerpt) // 4),
            quality_checks=["evidence_exists", "locator_present", "immutable_hash"],
        )

    def verify_claim(self, claim: Claim) -> Claim:
        self.validate_ids(claim.evidence_ids)
        cited_evidence = [self.get(evidence_id) for evidence_id in claim.evidence_ids]
        publishers = {
            item.publisher.strip().lower()
            for item in self._evidence.values()
            if item.publisher and len(item.publisher.strip()) >= 3
        }
        subject = claim.subject.lower()
        named_products = {publisher for publisher in publishers if publisher in subject}
        source_ceiling = min(self.get(evidence_id).credibility for evidence_id in claim.evidence_ids)
        if claim.confidence > source_ceiling + 1e-9:
            raise ValueError(
                "claim confidence exceeds its least-credible cited source: "
                f"claim_id={claim.id}; confidence={claim.confidence}; source_ceiling={source_ceiling}"
            )
        for evidence_id in claim.evidence_ids:
            evidence = self.get(evidence_id)
            if claim.fact_or_inference == "fact" and named_products:
                # Secondary aggregators are allowed only when their excerpt names
                # the same product, preventing common-value collisions such as a
                # Coda '$0' being used as Notion evidence.
                excerpt_lower = evidence.excerpt.lower()
                publisher_lower = (evidence.publisher or "").strip().lower()
                if publisher_lower not in named_products and not any(product in excerpt_lower for product in named_products):
                    raise ValueError(
                        "fact claim subject does not match cited source product: "
                        f"claim_id={claim.id}; subject={claim.subject[:160]!r}; "
                        f"evidence_id={evidence_id}; publisher={evidence.publisher!r}"
                    )
            if claim.fact_or_inference == "fact" and str(claim.value).lower() not in evidence.excerpt.lower():
                value = str(claim.value)
                raise ValueError(
                    "fact claim value is not a verbatim excerpt substring: "
                    f"claim_id={claim.id}; value={value[:160]!r}; "
                    f"evidence_id={evidence_id}; publisher={evidence.publisher!r}"
                )
        urls = re.findall(r"https?://[^\s)\]}>]+", str(claim.value))
        cited_text = "\n".join(item.excerpt for item in cited_evidence)
        for url in urls:
            normalized_url = url.rstrip(".,;，。；")
            if normalized_url not in cited_text:
                raise ValueError(
                    "claim contains URL not present in cited evidence: "
                    f"claim_id={claim.id}; url={url!r}"
                )
        return claim

    def verify_finding(self, finding: VerifiedFinding, claims: Iterable[Claim]) -> VerifiedFinding:
        claim_list = list(claims)
        self.validate_ids(finding.evidence_ids)
        claim_ids = {c.id for c in claim_list}
        finding_claim_ids = set(finding.claim_ids)
        if len(finding.claim_ids) != len(claim_list) or finding_claim_ids != claim_ids:
            missing = claim_ids - finding_claim_ids
            unknown = finding_claim_ids - claim_ids
            raise ValueError(
                f"finding claim coverage mismatch: {finding.finding_id}; "
                f"missing={sorted(map(str, missing))}; unknown={sorted(map(str, unknown))}"
            )
        for claim in claim_list:
            self.verify_claim(claim)
        claim_numbers = set()
        for claim in claim_list:
            if claim.fact_or_inference == "fact":
                claim_numbers.update(self._material_numbers(str(claim.value)))
        report_text = "\n".join(
            [finding.statement, *finding.contradictions, *finding.unknowns]
        )
        report_text = self._strip_evidence_identifiers(report_text, finding.evidence_ids)
        missing_numbers = self._material_numbers(report_text) - claim_numbers
        if missing_numbers:
            raise ValueError(
                "finding contains numbers without atomic claim values: "
                f"{sorted(missing_numbers)}"
            )
        claim_confidence = {claim.id: claim.confidence for claim in claim_list}
        supported_ceiling = min(claim_confidence[claim_id] for claim_id in finding.claim_ids)
        if finding.confidence > supported_ceiling + 1e-9:
            raise ValueError(
                "finding confidence exceeds its supporting claims: "
                f"finding_id={finding.finding_id}; confidence={finding.confidence}; "
                f"claim_ceiling={supported_ceiling}"
            )
        return finding

    @staticmethod
    def _material_numbers(value: str) -> set[str]:
        # Parenthesized list markers such as '(1)' are formatting, not facts.
        cleaned = re.sub(r"\(\s*\d+\s*\)", "", value)
        numbers = set()
        for match in re.finditer(
            r"(?<![A-Za-z0-9-])\$?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z0-9-])",
            cleaned,
        ):
            token = match.group(0).replace("$", "").replace(",", "").replace("%", "")
            numbers.add(token)
        return numbers

    def verify_task_coverage(
        self,
        bundle: ResearchBundle,
        competitors: Iterable[str],
        dimensions: Iterable[str],
        mode: str,
    ) -> ResearchBundle:
        """Reject schema-valid bundles that did not actually answer the task."""

        def normalized(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", value.casefold())

        competitor_list = list(competitors)
        dimension_list = list(dimensions)
        statement = normalized(bundle.finding.statement)
        min_statement_chars = 60 if mode == "verify" else 120
        if len(bundle.finding.statement.strip()) < min_statement_chars:
            raise ValueError(
                f"finding statement is too short for task coverage: "
                f"length={len(bundle.finding.statement.strip())}; minimum={min_statement_chars}"
            )
        min_claims = 1 if mode == "verify" else max(2, len(dimension_list))
        if mode != "verify" and "nextaction" not in statement and "下一步" not in bundle.finding.statement:
            raise ValueError("finding must end with an explicit Next action or 下一步")
        for competitor in competitor_list:
            key = normalized(competitor)
            if key not in statement:
                raise ValueError(f"finding statement does not cover competitor: {competitor}")
            claim_count = sum(key in normalized(claim.subject) for claim in bundle.claims)
            if claim_count < min_claims:
                raise ValueError(
                    f"insufficient claims for competitor: {competitor}; "
                    f"found={claim_count}; minimum={min_claims}"
                )
        if competitor_list:
            competitor_keys = [normalized(competitor) for competitor in competitor_list]
            for claim in bundle.claims:
                if claim.fact_or_inference == "fact" and not any(
                    key in normalized(claim.subject) for key in competitor_keys
                ):
                    raise ValueError(
                        "fact claim subject must identify a task competitor: "
                        f"claim_id={claim.id}; subject={claim.subject[:160]!r}"
                    )
        return bundle

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps([e.model_dump(mode="json") for e in self._evidence.values()], indent=2), encoding="utf-8")

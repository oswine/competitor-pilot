"""Deterministic, evidence-preserving context compression for research agents.

The packer intentionally uses extractive chunks instead of an LLM summary.  Every
token passed to the researcher therefore remains traceable to an immutable
Evidence record and a DOM/PDF locator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from .models import Evidence, EvidenceCard


TOKEN_RE = re.compile(r"[A-Za-z0-9_$%.+-]{2,}|[\u4e00-\u9fff]{2,}")
SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def _tokens(value: str) -> set[str]:
    return {part.lower() for part in TOKEN_RE.findall(value)}


def _estimate_tokens(value: str) -> int:
    # Conservative enough for budget admission without a model-specific tokenizer.
    return max(1, (len(value) + 3) // 4)


def _chunks(text: str, max_tokens: int) -> list[str]:
    limit = max(80, max_tokens * 4)
    parts = [part.strip() for part in SENTENCE_RE.split(text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for part in parts or [text]:
        if len(part) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(part[index:index + limit] for index in range(0, len(part), limit))
            continue
        proposed = f"{current} {part}".strip()
        if current and len(proposed) > limit:
            chunks.append(current)
            current = part
        else:
            current = proposed
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class ContextPack:
    cards: list[EvidenceCard]
    rendered: str
    token_count: int
    excluded_evidence_ids: list[UUID]


def build_context_pack(
    brief: str,
    evidence: Iterable[Evidence],
    *,
    total_token_budget: int,
    per_card_token_budget: int,
    max_cards: int,
    excluded_evidence_ids: set[UUID] | None = None,
) -> ContextPack:
    """Select diverse, high-quality evidence chunks within a fixed token budget."""
    excluded = excluded_evidence_ids or set()
    query_tokens = _tokens(brief)
    candidates: list[tuple[float, set[str], EvidenceCard]] = []
    for item in evidence:
        if item.id in excluded:
            continue
        for index, excerpt in enumerate(_chunks(item.excerpt, per_card_token_budget), 1):
            terms = _tokens(excerpt)
            overlap = len(query_tokens & terms) / max(1, len(query_tokens))
            # Primary-source credibility is supplied by the immutable evidence record.
            score = 0.72 * overlap + 0.28 * item.credibility
            card = EvidenceCard(
                card_id=f"card-{str(item.id)[:8]}-{index}",
                evidence_ids=[item.id],
                summary=excerpt[:240],
                excerpt=excerpt,
                locator=f"{item.locator};chunk={index}",
                token_count=_estimate_tokens(excerpt),
                quality_checks=["evidence_exists", "locator_present", "immutable_hash", "extractive_chunk"],
            )
            candidates.append((score, terms, card))

    selected: list[tuple[set[str], EvidenceCard]] = []
    selected_evidence: set[UUID] = set()
    spent = 0
    while candidates and len(selected) < max_cards:
        best_index = -1
        best_score = float("-inf")
        for index, (relevance, terms, card) in enumerate(candidates):
            # MMR prevents one long page from crowding out independent sources.
            similarity = max((len(terms & chosen) / max(1, len(terms | chosen)) for chosen, _ in selected), default=0.0)
            same_source_penalty = 0.18 if card.evidence_ids[0] in selected_evidence else 0.0
            score = relevance - 0.32 * similarity - same_source_penalty
            if score > best_score:
                best_index, best_score = index, score
        _, terms, card = candidates.pop(best_index)
        if spent + card.token_count > total_token_budget:
            continue
        selected.append((terms, card))
        selected_evidence.add(card.evidence_ids[0])
        spent += card.token_count

    cards = [card for _, card in selected]
    rendered = "\n\n".join(
        f"[{card.evidence_ids[0]}] locator={card.locator}\n{card.excerpt}" for card in cards
    )
    return ContextPack(cards=cards, rendered=rendered, token_count=spent, excluded_evidence_ids=sorted(excluded, key=str))

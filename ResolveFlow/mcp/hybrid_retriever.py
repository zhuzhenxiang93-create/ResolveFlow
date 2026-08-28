"""Deterministic lexical retrieval and rank fusion for ResolveFlow RAG.

The lexical side deliberately has no network or LLM dependency.  It uses BM25
over a tokenizer that keeps exact identifiers intact and emits Chinese
unigrams/bigrams, so English words, mixed-language queries and opaque support
codes can share one index.
"""
from __future__ import annotations

import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set


_IDENTIFIER_RE = re.compile(
    r"(?<![a-z0-9])[a-z0-9]+(?:[-_.:/][a-z0-9]+)+(?![a-z0-9])",
    re.IGNORECASE,
)
_LATIN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def normalize_text(text: Any) -> str:
    """Normalize compatibility characters without destroying identifiers."""
    return unicodedata.normalize("NFKC", str(text or "")).lower().strip()


def extract_identifiers(text: Any) -> Set[str]:
    """Return opaque identifiers such as ``ERR_X91`` and ``RTX-5090``."""
    return set(_IDENTIFIER_RE.findall(normalize_text(text)))


def contains_cjk(text: Any) -> bool:
    return bool(_CJK_RE.search(normalize_text(text)))


def tokenize(text: Any) -> List[str]:
    """Tokenize Chinese, English and exact identifiers for BM25.

    Chinese has no whitespace boundary in most support queries, so we emit
    characters and bigrams.  Exact mixed identifiers are additionally emitted
    as whole tokens; their Latin/numeric components remain searchable too.
    """
    normalized = normalize_text(text)
    tokens: List[str] = []
    tokens.extend(f"id:{value}" for value in _IDENTIFIER_RE.findall(normalized))
    tokens.extend(_LATIN_RE.findall(normalized))
    for segment in _CJK_RE.findall(normalized):
        tokens.extend(segment)
        tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        if len(segment) <= 8:
            tokens.append(segment)
    return tokens


class BM25Index:
    """Thread-safe incremental BM25 index keyed by stable ``chunk_id``."""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75, exact_boost: float = 4.0):
        self.k1 = float(k1)
        self.b = float(b)
        self.exact_boost = float(exact_boost)
        self._records: Dict[str, Dict[str, Any]] = {}
        self._term_frequencies: Dict[str, Counter[str]] = {}
        self._doc_lengths: Dict[str, int] = {}
        self._document_frequencies: Counter[str] = Counter()
        self._postings: Dict[str, Set[str]] = defaultdict(set)
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    @staticmethod
    def _indexable_text(record: Mapping[str, Any]) -> str:
        metadata = record.get("metadata") or {}
        terms = metadata.get("search_terms", [])
        if isinstance(terms, str):
            try:
                import json

                terms = json.loads(terms)
            except (TypeError, ValueError):
                terms = [terms]
        terms_text = " ".join(str(term) for term in terms or [])
        title = str(record.get("title", ""))
        content = str(record.get("content", ""))
        # Repetition is a simple field boost while preserving standard BM25.
        return f"{title} {title} {terms_text} {terms_text} {terms_text} {content}"

    def _remove_chunk(self, chunk_id: str) -> None:
        old_tf = self._term_frequencies.pop(chunk_id, None)
        if old_tf is None:
            return
        for term in old_tf:
            self._document_frequencies[term] -= 1
            if self._document_frequencies[term] <= 0:
                del self._document_frequencies[term]
            self._postings[term].discard(chunk_id)
            if not self._postings[term]:
                del self._postings[term]
        self._doc_lengths.pop(chunk_id, None)
        self._records.pop(chunk_id, None)

    def upsert(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Incrementally add or replace records without rebuilding per query."""
        with self._lock:
            for raw in records:
                record = dict(raw)
                chunk_id = str(record.get("chunk_id", "")).strip()
                if not chunk_id:
                    raise ValueError("BM25 record requires a stable chunk_id")
                self._remove_chunk(chunk_id)
                term_frequency = Counter(tokenize(self._indexable_text(record)))
                self._records[chunk_id] = record
                self._term_frequencies[chunk_id] = term_frequency
                self._doc_lengths[chunk_id] = sum(term_frequency.values())
                for term in term_frequency:
                    self._document_frequencies[term] += 1
                    self._postings[term].add(chunk_id)

    def remove(self, chunk_ids: Iterable[str]) -> None:
        with self._lock:
            for chunk_id in chunk_ids:
                self._remove_chunk(str(chunk_id))

    def search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        if top_k <= 0:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        with self._lock:
            total_documents = len(self._records)
            if not total_documents:
                return []
            candidates: Set[str] = set()
            for term in set(query_tokens):
                candidates.update(self._postings.get(term, ()))
            if not candidates:
                return []
            average_length = sum(self._doc_lengths.values()) / total_documents
            query_frequency = Counter(query_tokens)
            query_identifiers = extract_identifiers(query)
            scored = []
            for chunk_id in candidates:
                term_frequency = self._term_frequencies[chunk_id]
                document_length = self._doc_lengths[chunk_id]
                score = 0.0
                for term, query_count in query_frequency.items():
                    frequency = term_frequency.get(term, 0)
                    if not frequency:
                        continue
                    document_frequency = self._document_frequencies[term]
                    inverse_document_frequency = math.log(
                        1.0 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5)
                    )
                    denominator = frequency + self.k1 * (
                        1.0 - self.b + self.b * document_length / max(average_length, 1.0)
                    )
                    score += query_count * inverse_document_frequency * (
                        frequency * (self.k1 + 1.0) / denominator
                    )
                searchable = normalize_text(self._indexable_text(self._records[chunk_id]))
                exact_hits = sum(identifier in searchable for identifier in query_identifiers)
                score += exact_hits * self.exact_boost
                if score > 0:
                    record = dict(self._records[chunk_id])
                    record["lexical_score"] = round(score, 6)
                    record["exact_identifier_hits"] = exact_hits
                    scored.append(record)
            scored.sort(
                key=lambda item: (-float(item["lexical_score"]), str(item["chunk_id"]))
            )
            return scored[:top_k]


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    top_k: int,
    c: float = 60.0,
    route_weights: Mapping[str, float] | None = None,
) -> List[Dict[str, Any]]:
    """Fuse independent rankings by stable chunk ID using weighted RRF.

    With no ``route_weights`` this is standard RRF.  Route reliability may be
    calibrated for a known corpus/language without comparing incompatible raw
    BM25 and vector scores.
    """
    if top_k <= 0:
        return []
    if c < 0:
        raise ValueError("RRF c must be non-negative")
    merged: Dict[str, Dict[str, Any]] = {}
    for route, items in rankings.items():
        weight = float((route_weights or {}).get(route, 1.0))
        if weight < 0:
            raise ValueError("RRF route weights must be non-negative")
        seen_in_route: Set[str] = set()
        for rank, raw in enumerate(items, start=1):
            chunk_id = str(raw.get("chunk_id", "")).strip()
            if not chunk_id or chunk_id in seen_in_route:
                continue
            seen_in_route.add(chunk_id)
            target = merged.setdefault(chunk_id, dict(raw))
            # Fill fields supplied only by the other route without overwriting
            # the canonical content/metadata selected first.
            for key, value in raw.items():
                if target.get(key) is None and value is not None:
                    target[key] = value
            target[f"{route}_rank"] = rank
            route_score = raw.get(f"{route}_score")
            if route_score is not None:
                target[f"{route}_score"] = route_score
            target["rrf_score"] = (
                float(target.get("rrf_score", 0.0)) + weight / (c + rank)
            )

    fused = list(merged.values())
    for item in fused:
        item.setdefault("vector_rank", None)
        item.setdefault("lexical_rank", None)
        item.setdefault("vector_score", None)
        item.setdefault("lexical_score", None)
        item["rrf_score"] = round(float(item["rrf_score"]), 8)
        item["score"] = item["rrf_score"]
        item["semantic_score"] = item["vector_score"]
    fused.sort(
        key=lambda item: (
            -float(item["rrf_score"]),
            min(item.get("vector_rank") or math.inf, item.get("lexical_rank") or math.inf),
            str(item["chunk_id"]),
        )
    )
    return fused[:top_k]

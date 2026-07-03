"""
sieveon - LightMem-style Entropy Gate
Composite Score aus Text-Entropy und Embedding-Novelty
Nur vor KG-Write, Raw Event Log bekommt immer alles!
"""

import hashlib
import json
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

import numpy as np
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import our embedding service
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import sys as _sys

from src.extraction.embedding_service import BaseEmbeddingService, get_embedding_service
from src.extraction.entity_utils import (
    extract_noun_phrases,
    infer_entity_type,
    is_content_phrase,
)


def escape_surrealql(value: str) -> str:
    """Escape a string for safe use in a SurrealQL string literal."""
    import re

    value = value.replace("\\", "\\\\")
    value = value.replace("'", "\\'")
    value = value.replace("}", "\\}")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    value = value.replace("\t", "\\t")
    value = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", value)
    return value


def _debug_print(*args, **kwargs):
    """Print to stderr to avoid breaking MCP JSON-RPC on stdout."""
    print(*args, file=_sys.stderr, **kwargs)


class EntropyGateConfig:
    def __init__(
        self,
        alpha: float = 0.35,  # Gewicht Text-Entropy
        beta: float = 0.65,  # Gewicht Embedding-Novelty
        base_threshold: float = 0.30,  # Minimum threshold (cold start)
        max_threshold: float = 0.55,  # Maximum threshold (mature DB)
        ramp_events: int = 150,  # Events needed to reach max_threshold
        min_length: int = 10,  # Unter X Zeichen immer skippen
        min_diversity: float = 0.15,  # Anteil unique chars; repetitive Texte darunter skippen
        max_length: int = 1000,  # Über X Zeichen immer skippen
        max_entities_for_cooccurrence: int = 6,  # Obergrenze gegen kombinatorische Explosion
    ):
        self.alpha = alpha
        self.beta = beta
        self.base_threshold = base_threshold
        self.max_threshold = max_threshold
        self.ramp_events = ramp_events
        self.min_length = min_length
        self.max_length = max_length
        self.min_diversity = min_diversity
        self.max_entities_for_cooccurrence = max_entities_for_cooccurrence


class EntropyGate:
    def __init__(
        self,
        embedding_service: Optional[BaseEmbeddingService] = None,
        config: Optional[EntropyGateConfig] = None,
    ):
        self.config = config or EntropyGateConfig()
        self.min_length = self.config.min_length
        self.max_length = self.config.max_length
        self.surreal_url = os.getenv("SURREALDB_URL", "http://127.0.0.1:8000/sql")
        self.auth = (
            os.getenv("SURREALDB_USER", "root"),
            os.getenv("SURREALDB_PASS", "root"),
        )
        self.surreal_ns = os.getenv("SURREALDB_NS", "sieveon")
        self.surreal_db = os.getenv("SURREALDB_DB", "sieveon")
        self.embedding_service = embedding_service or get_embedding_service()

    def _query_surreal(self, sql: str) -> List[Dict]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "text/plain; charset=utf-8",
        }
        full_sql = f"USE NS {self.surreal_ns} DB {self.surreal_db};\n{sql}"
        response = requests.post(
            self.surreal_url,
            data=full_sql.encode("utf-8"),
            headers=headers,
            auth=self.auth,
            timeout=30,
        )
        try:
            data = response.json()
            if isinstance(data, list):
                return data
            else:
                return [data]
        except Exception as e:
            print(f"SurrealDB query failed: {e}")
            print(f"Response: {response.text}")
            return []

    def _escape_surrealql(self, value: str) -> str:
        return escape_surrealql(value)

    @staticmethod
    def _extract_result(data: List[Dict], index: int = 1) -> List[Dict]:
        """Safely extract the result list from a SurrealDB multi-statement response."""
        if not isinstance(data, list) or len(data) <= index:
            return []
        item = data[index]
        if not isinstance(item, dict):
            return []
        result = item.get("result", [])
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        return []

    @staticmethod
    def _extract_ok(data: List[Dict], index: int = 1) -> bool:
        """Safely check if a SurrealDB statement returned status OK."""
        if not isinstance(data, list) or len(data) <= index:
            return False
        item = data[index]
        return isinstance(item, dict) and item.get("status") == "OK"

    def _hash_content(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def calculate_char_entropy(self, text: str) -> float:
        """
        Shannon-Entropy auf Zeichenebene (wie LightMem)
        Returns *unnormalized* entropy (typically 0-4.5)
        """
        if not text:
            return 0.0
        freq = {}
        chars = list(text.lower())
        n = len(chars)
        if n == 0:
            return 0.0
        for c in chars:
            if c.isalnum() or c.isspace():
                freq[c] = freq.get(c, 0) + 1
        total = sum(freq.values())
        if total == 0:
            return 0.0
        entropy = 0.0
        for count in freq.values():
            p = count / total
            entropy -= p * math.log2(p)
        return entropy

    def calculate_novelty(self, text: str, content_hash: Optional[str] = None) -> float:
        """
        Novelty based on embedding similarity against existing content in SurrealDB.
        Returns value between 0 (no novelty) and 1 (maximum novelty).
        Uses SurrealDB's native vector functions.
        content_hash: if provided, excludes the event with this hash (prevents self-match).
        """
        if not text.strip():
            return 0.0

        # Generate embedding for the text
        embedding = self.embedding_service.embed_for_storage(text)
        emb_str = "[" + ", ".join(map(str, embedding)) + "]"

        # Search for top-k similar vectors in SurrealDB
        # We use cosine similarity and calculate 1.0 - avg_similarity for novelty
        exclude_self = ""
        if content_hash:
            exclude_self = f"\n  AND content_hash != '{content_hash}'"
        sql = f"""
        SELECT vector::similarity::cosine(embedding, {emb_str}) AS similarity
        FROM event
        WHERE embedding IS NOT NONE
          AND array::len(embedding) = {len(embedding)}
          AND (forgotten IS NONE OR forgotten = false){exclude_self}
        ORDER BY similarity DESC
        LIMIT 5;
        """

        results = self._query_surreal(sql)

        actual_results = self._extract_result(results)

        if not actual_results:
            # No similar content found - maximum novelty
            return 1.0

        # Calculate average similarity (lower = more novel)
        avg_similarity = sum(float(r["similarity"]) for r in actual_results) / len(
            actual_results
        )

        # Return inverse (novelty = 1 - similarity)
        return 1.0 - max(0.0, min(1.0, avg_similarity))

    def _count_events(self) -> int:
        """Count total stored events (for adaptive threshold)."""
        try:
            sql = "SELECT count() AS c FROM event WHERE (forgotten IS NONE OR forgotten = false) LIMIT 1;"
            result = self._query_surreal(sql)
            rows = self._extract_result(result)
            if rows:
                return rows[0].get("c", 0)
        except Exception:
            pass
        return 0

    def _get_adaptive_threshold(self) -> float:
        """Threshold steigt linear von base_threshold → max_threshold mit der Event-Anzahl."""
        n = self._count_events()
        bt = self.config.base_threshold
        mt = self.config.max_threshold
        ramp = self.config.ramp_events
        if ramp <= 0:
            return mt
        fraction = min(n / ramp, 1.0)
        return bt + (mt - bt) * fraction

    @staticmethod
    def _character_diversity(text: str) -> float:
        """Anteil unique chars: len(set(text)) / len(text). < 0.30 = repetitive."""
        if not text:
            return 0.0
        unique = len(set(text.lower()))
        return unique / len(text)

    @staticmethod
    def _word_diversity(text: str) -> float:
        """Word-level diversity: len(set(words)) / len(words). < 0.20 = repetitive."""
        words = text.lower().split()
        if not words:
            return 0.0
        return len(set(words)) / len(words)

    def should_extract(
        self, text: str, content_hash: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Entscheidet basierend auf Composite-Score ob Text in KG extrahiert werden soll
        content_hash: wird an calculate_novelty weitergereicht, um Self-Match zu verhindern
        """
        if len(text) < self.config.min_length:
            result = {
                "decision": "skip",
                "reason": "text_too_short",
                "text_length": len(text),
                "min_length": self.config.min_length,
            }
            self._log_decision(
                text,
                0.0,
                0.0,
                0.0,
                result["decision"],
                reason_override=result["reason"],
            )
            return result
        if len(text) > self.config.max_length:
            result = {
                "decision": "skip",
                "reason": "text_too_long",
                "text_length": len(text),
                "max_length": self.config.max_length,
            }
            self._log_decision(
                text,
                0.0,
                0.0,
                0.0,
                result["decision"],
                reason_override=result["reason"],
            )
            return result

        # Diversity-Check: character-level für Kurztexte, word-level für längere
        # Character diversity skaliert nicht mit Textlänge (Englisch hat nur ~36 unique chars)
        if len(text) <= 150:
            diversity = self._character_diversity(text)
            threshold = self.config.min_diversity
            if diversity < threshold:
                result = {
                    "decision": "skip",
                    "reason": "too_repetitive",
                    "character_diversity": diversity,
                    "threshold": threshold,
                }
                self._log_decision(
                    text,
                    0.0,
                    0.0,
                    0.0,
                    result["decision"],
                    reason_override=result["reason"],
                )
                return result
        else:
            diversity = self._word_diversity(text)
            threshold = 0.20
            if diversity < threshold:
                result = {
                    "decision": "skip",
                    "reason": "too_repetitive",
                    "word_diversity": diversity,
                    "threshold": threshold,
                }
                self._log_decision(
                    text,
                    0.0,
                    0.0,
                    0.0,
                    result["decision"],
                    reason_override=result["reason"],
                )
                return result

        # Calculate individual scores
        text_entropy = self.calculate_char_entropy(text)
        novelty = self.calculate_novelty(text, content_hash=content_hash)

        # Normalize entropy to 0-1 range (assuming max entropy of ~4.5)
        normalized_entropy = min(text_entropy / 4.5, 1.0)

        # Calculate composite score
        composite_score = (self.config.alpha * normalized_entropy) + (
            self.config.beta * novelty
        )

        # Adaptive threshold based on DB maturity
        threshold = self._get_adaptive_threshold()

        # Make decision
        decision = "extract" if composite_score >= threshold else "ignore"

        # Log decision to database
        self._log_decision(text, normalized_entropy, novelty, composite_score, decision)

        return {
            "decision": decision,
            "text_entropy": text_entropy,
            "normalized_entropy": normalized_entropy,
            "novelty": novelty,
            "composite_score": composite_score,
            "threshold": threshold,
            "alpha": self.config.alpha,
            "beta": self.config.beta,
            "reason": f"Composite score {composite_score:.3f} {'meets' if decision == 'extract' else 'does not meet'} threshold {threshold:.3f}",
        }

    def _log_decision(
        self,
        text: str,
        entropy: float,
        novelty: float,
        composite_score: float,
        decision: str,
        reason_override: Optional[str] = None,
    ):
        """Log the entropy gate decision to database"""
        try:
            content_hash = self._hash_content(text)
            decision_escaped = self._escape_surrealql(decision)
            threshold = self._get_adaptive_threshold()
            if reason_override:
                reason = reason_override
            else:
                reason = f"Composite score {composite_score:.3f} {'meets' if decision == 'extract' else 'does not meet'} threshold {threshold:.3f}"
            reason_escaped = self._escape_surrealql(reason)
            sql = f"""
            CREATE gate_log SET
                content_hash = '{content_hash}',
                text_score = {entropy},
                novelty = {novelty},
                gate_score = {composite_score},
                decision = '{decision_escaped}',
                reason = '{reason_escaped}',
                threshold = {threshold};
            """
            result = self._query_surreal(sql)
            if not self._extract_ok(result):
                import sys

                sys.stderr.write(f"[Gate] _log_decision failed: {result}\n")
        except Exception as e:
            import sys

            sys.stderr.write(f"[Gate] _log_decision exception: {e}\n")

    def _extract_candidate_entities(self, text: str) -> List[str]:
        """
        Extract candidate entities from text.
        Uses spaCy NER as primary source, with regex fallback.
        """
        import re as _re

        # Try Groq/spaCy-based extraction first
        try:
            from src.extraction.entity_utils import extract_entities

            entities = extract_entities(text)
            candidates = {entity["name"] for entity in entities}
        except ImportError:
            # If neither is available, fall back to original regex approach
            candidates = set()

        # If spaCy is not available or returns no entities, use original approach as fallback
        if not candidates:
            # 1. Noun-Phrase-Extraktion
            noun_phrases = extract_noun_phrases(text)
            for phrase in noun_phrases:
                candidates.add(phrase)

            # 2. Kleingeschriebene mehrteilige Konzepte (z.B. "quantum computing", "social contract")
            concept_patterns = [
                _re.compile(r"\b[a-z]{3,}(?:\s+[a-z]{3,}){1,2}\b"),
            ]
            for pattern in concept_patterns:
                for match in pattern.finditer(text):
                    candidate = match.group(0).strip()
                    words = candidate.split()
                    if len(words) >= 2 and len(candidate) >= 5:
                        if is_content_phrase(words):
                            candidates.add(candidate)

            # 3. CamelCase-Wörter (z.B. "FastMCP", "SurrealDB")
            for match in _re.finditer(r"\b([A-Z][a-z]+[A-Z][a-zA-Z]*)\b", text):
                candidate = match.group(1).strip()
                if len(candidate) >= 3:
                    candidates.add(candidate)

            # 4. Abkürzungen (z.B. "API", "NER", "KG")
            for match in _re.finditer(r"\b([A-Z]{2,})\b", text):
                candidate = match.group(1).strip()
                if len(candidate) >= 2:
                    candidates.add(candidate)

        return list(candidates)

    def _compute_relation_confidence(
        self, text: str, entity_a: str, entity_b: str,
        emb_a: Optional[List[float]] = None,
        emb_b: Optional[List[float]] = None,
    ) -> tuple[str, float]:
        """Berechne dynamische Konfidenz und Relationstyp zwischen zwei Entities basierend auf Embedding.
        Wenn emb_a/emb_b übergeben werden, werden diese genutzt (erspart wiederholte Embedding-Calls)."""
        emb_service = self.embedding_service
        try:
            if emb_a is None:
                emb_a = emb_service.embed_for_storage(entity_a)
            if emb_b is None:
                emb_b = emb_service.embed_for_storage(entity_b)
            dot = sum(a * b for a, b in zip(emb_a, emb_b))
            norm = (sum(a * a for a in emb_a) ** 0.5) * (
                sum(b * b for b in emb_b) ** 0.5
            )
            similarity = dot / norm if norm > 0 else 0.0
            confidence = max(0.0, min(1.0, similarity))
            label = self._infer_relation_type(text, entity_a, entity_b, confidence)
            return label, confidence
        except Exception:
            return "co_occurs_with", 0.5

    def _infer_relation_type(
        self, text: str, entity_a: str, entity_b: str, confidence: float
    ) -> str:
        """Determine the relationship type between two entities.
        Validates against ontology to avoid nonsensical predicates."""
        from src.extraction.entity_utils import infer_entity_type, validate_predicate

        a_type = infer_entity_type(entity_a, self.embedding_service)
        b_type = infer_entity_type(entity_b, self.embedding_service)

        lower_text = text.lower()
        a_lower = entity_a.lower()
        b_lower = entity_b.lower()

        works_verbs = ["works at", "works for", "employed by", "joined", "led by"]
        for verb in works_verbs:
            if verb in lower_text:
                parts = lower_text.split(verb, 1)
                if len(parts) >= 2 and a_lower in parts[0]:
                    if validate_predicate(a_type, "works_at", b_type):
                        return "works_at"

        located_verbs = ["located in", "based in", "situated in"]
        for verb in located_verbs:
            if verb in lower_text:
                if validate_predicate(a_type, "located_in", b_type):
                    return "located_in"

        created_verbs = ["created", "developed", "built", "founded", "implemented"]
        for verb in created_verbs:
            if verb in lower_text:
                if validate_predicate(a_type, "created", b_type):
                    return "created"

        if confidence > 0.85:
            return "strongly_related"
        elif confidence > 0.7:
            return "related_to"
        elif confidence > 0.55:
            return "co_occurs_with"
        else:
            return "weakly_related"

    def _ensure_entity(
        self, name: str, preferred_type: Optional[str] = None
    ) -> Optional[str]:
        """Create an entity if it does not exist. Returns the entity ID."""
        name_escaped = self._escape_surrealql(name)
        entity_type = preferred_type or infer_entity_type(name, self.embedding_service)

        # 1. Exact name match (fastest path)
        check_sql = f"SELECT id FROM entity WHERE name = '{name_escaped}' LIMIT 1;"
        check_result = self._query_surreal(check_sql)
        existing = self._extract_result(check_result)
        if existing and len(existing) > 0:
            return existing[0].get("id")

        # 2. Embedding similarity (via SurrealDB Vector Index)
        similar = self._find_similar_entity(name, threshold=0.85)
        if similar:
            return similar

        # 3. Create new entity with embedding
        try:
            embedding = self.embedding_service.embed_for_storage(name)
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
            create_sql = f"""
            CREATE entity SET
                name = '{name_escaped}',
                type = '{entity_type}',
                created_at = time::now(),
                updated_at = time::now(),
                embedding = {embedding_str};
            """
        except Exception:
            create_sql = None

        if create_sql:
            result = self._query_surreal(create_sql)
            entity_result = self._extract_result(result)
            if entity_result and len(entity_result) > 0:
                return entity_result[0].get("id")

        # Fallback: create entity without embedding
        fallback_sql = f"""
        CREATE entity SET
            name = '{name_escaped}',
            type = '{entity_type}',
            created_at = time::now(),
            updated_at = time::now();
        """
        result = self._query_surreal(fallback_sql)
        entity_result = self._extract_result(result)
        if entity_result and len(entity_result) > 0:
            return entity_result[0].get("id")
        return None

    def _find_similar_entity(self, name: str, threshold: float = 0.85) -> Optional[str]:
        """Find similar entity using SurrealDB vector similarity search."""
        try:
            emb = self.embedding_service.embed_for_storage(name)
            emb_str = "[" + ",".join(str(v) for v in emb) + "]"
            sql = f"""
            SELECT id, name, type,
                vector::similarity::cosine(embedding, {emb_str}) AS sim
            FROM entity
            WHERE embedding IS NOT NULL
              AND vector::similarity::cosine(embedding, {emb_str}) > {threshold}
            ORDER BY sim DESC
            LIMIT 1;
            """
            result = self._query_surreal(sql)
            entities = self._extract_result(result)
            if entities and len(entities) > 0:
                return entities[0].get("id")
        except Exception as e:
            # If embedding fails, skip similarity search
            pass
        return None

    def _select_salient_entities(
        self, text: str, entity_names: list[str], max_entities: int
    ) -> list[str]:
        """Select the most salient entities when count exceeds max_entities_for_cooccurrence.
        Scores by: frequency in text (weight 2) + specificity/multi-word bonus (weight 1)."""
        if len(entity_names) <= max_entities:
            return entity_names
        text_lower = text.lower()
        scored = []
        for name in entity_names:
            freq = text_lower.count(name.lower())
            specificity = len(name.split())
            scored.append((freq * 2 + specificity, name))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [name for _, name in scored[:max_entities]]

    def _dedup_candidates(self, candidates: List[str]) -> List[str]:
        """Deduplicate: keep the shortest canonical name, remove longer descriptive phrases."""
        result = []
        sorted_c = sorted(candidates, key=lambda x: len(x))
        for c in sorted_c:
            cl = c.lower().strip()
            is_superstr = False
            for other in result:
                ol = other.lower().strip()
                if cl != ol and len(cl) > len(ol) and ol in cl:
                    is_superstr = True
                    break
            if not is_superstr:
                result.append(c)
        return result

    _NOISE_WORDS = {
        "access",
        "assistance",
        "author",
        "bureaucracy",
        "changes",
        "deadlines",
        "effort",
        "efforts",
        "factors",
        "first",
        "gap",
        "governance",
        "injection",
        "june",
        "may",
        "march",
        "april",
        "performance",
        "planning",
        "poisoning",
        "progress",
        "security",
        "solutions",
        "testing",
        "vectors",
        "pricing",
        "focus",
        "nano",
        "cli",
        "os",
        "flash",
        "code",
        "agents",
        "agent",
        "models",
        "model",
        "framework",
        "platform",
        "service",
        "services",
        "system",
        "systems",
        "data",
        "time",
        "way",
        "part",
        "parts",
        "result",
        "results",
        "value",
        "values",
        "level",
        "levels",
        "rate",
        "rates",
        "cost",
        "costs",
        "price",
        "prices",
        "market",
        "provider",
        "providers",
        "standard",
        "network",
        "application",
        "applications",
        "user",
        "users",
        "tool",
        "tools",
        "process",
        "team",
        "teams",
        "work",
        "support",
        "report",
        "reports",
        "risk",
        "risks",
        "threat",
        "threats",
        "skill",
        "skills",
        "task",
        "tasks",
        "project",
        "projects",
        "security",
        "governance",
        "guardrails",
        "compliance",
        "bottleneck",
        "hindrance",
        "four",
        "they",
        "codebases",
        "coding",
        "environments",
        "orchestrator",
        "validation",
        "tests",
        "test",
    }

    def _filter_noisy_candidates(self, candidates: List[str]) -> List[str]:
        """Filter out noisy/unwanted entity candidates before KG insertion."""
        result = []
        for c in candidates:
            clean = c.strip(" \t\n\r.,;:!?()[]{}'")
            if len(clean) < 3:
                continue

            # Pure numbers
            if re.match(r"^\d+(?:\.\d+)?%?$", clean):
                continue

            # Currency amounts
            if re.match(r"^[\$€£¥]\s*\d+[\.\d,]*\s*[A-Za-z/]*", clean):
                continue

            # Measurements (digits + unit)
            if re.match(
                r"^[\d,.]+\s*(?:miles?|km|kg|gb|mb|tb|ghz|mhz|years?|days?|hours?|b|m)\b",
                clean.lower(),
            ):
                continue

            # Numeric prefixes (e.g. "3 Nano", "309B", "30B", "40 cities")
            if re.match(r"^[\d,.%]+\s+", clean):
                continue

            # Digits-only or mostly numeric
            digit_count = sum(1 for ch in clean if ch.isdigit())
            if digit_count > 0 and digit_count / max(len(clean), 1) > 0.4:
                continue

            # Parenthesis fragments
            if clean.startswith("("):
                continue

            # Single generic words
            words = clean.split()
            if (
                len(words) == 1
                and clean.lower().strip(".,;:!?()[]{}'") in self._NOISE_WORDS
            ):
                continue

            # Sentence fragments: multi-word starting with determiner/number/possessive, no proper nouns
            if len(words) >= 2:
                first = words[0].lower().strip(".,;:!?()[]{}'")
                if first in (
                    "the",
                    "a",
                    "an",
                    "this",
                    "that",
                    "these",
                    "those",
                    "any",
                    "no",
                    "its",
                    "one",
                    "two",
                    "three",
                    "four",
                    "five",
                    "six",
                    "seven",
                    "eight",
                    "nine",
                    "ten",
                ):
                    remaining = words[1:]
                    if not any(w[0].isupper() for w in remaining if w):
                        continue

            # Reject if entity ends with a measurement word (e.g. "78.0 percent", "2 cents", "3 dollars")
            if len(words) >= 2:
                last_word = words[-1].lower().strip('.,;:!?()[]{}""\'')
                if last_word in (
                    "percent",
                    "dollars",
                    "cents",
                    "euros",
                    "pounds",
                    "billion",
                    "million",
                    "miles",
                    "years",
                    "days",
                    "hours",
                    "tokens",
                    "parameters",
                ):
                    continue

            # 4+ word phrases with no proper nouns at all
            if len(words) >= 4:
                if not any(w[0].isupper() for w in words if w):
                    continue

            # Overlong candidates (>6 words) with lowercase verbs/internal stops → sentence fragment
            if len(words) > 6:
                continue

            # 5-6 word phrases starting with capitalized word but containing a lowercase verb → sentence fragment
            if len(words) >= 5 and words[0][0].isupper():
                lowercase_words = [w for w in words[1:] if w and w[0].islower()]
                if len(lowercase_words) >= 2:
                    continue

            result.append(c)
        return result

    def _extract_to_kg(self, text: str, event_id: str, debug: bool = False):
        """
        Extract entities and semantic relationships to the Knowledge Graph.
        Uses SVO extraction as primary method, with co-occurrence as fallback.
        """
        # First, try SVO extraction if spaCy is available
        entity_type_map = {}
        try:
            from src.extraction.entity_utils import extract_entities, extract_triples

            svo_triples = extract_triples(text)
            entities = extract_entities(text)

            # Extract entities from SVO triples if any
            svo_entities = set()
            for triple in svo_triples:
                svo_entities.add(triple["subject"])
                svo_entities.add(triple["object"])

            # Combine entities from SVO and spaCy NER, preserving types
            all_candidates = set()
            for entity in entities:
                name = entity["name"]
                all_candidates.add(name)
                if name not in entity_type_map:
                    entity_type_map[name] = entity["type"]
            all_candidates.update(svo_entities)

            candidates = self._dedup_candidates(list(all_candidates))
            candidates = self._filter_noisy_candidates(candidates)
        except ImportError:
            # Fall back to original method if spaCy is not available
            candidates = self._extract_candidate_entities(text)

        if not candidates:
            if debug:
                print(f"  [KG] No candidate entities found in text")
            return {"entities_created": 0, "facts_created": 0}

        if debug:
            print(f"  [KG] Found candidate entities: {candidates}")

        entities_created = 0
        facts_created = 0
        entity_ids = []
        entity_names = []

        for name in candidates:
            # Safety: skip if name doesn't appear in source text (hallucination guard)
            if name.lower() not in text.lower():
                if debug:
                    print(f"  [KG] Skipping hallucinated entity: {name}")
                continue
            eid = self._ensure_entity(name, entity_type_map.get(name))
            if eid:
                entity_ids.append(eid)
                entity_names.append(name)
                entities_created += 1
                if debug:
                    print(
                        f"  [KG] Entity: {name} ({entity_type_map.get(name, '?')}) -> {eid}"
                    )

        # Process SVO triples if spaCy is available
        try:
            from src.extraction.entity_utils import extract_triples

            svo_triples = extract_triples(text)

            # Create facts from SVO triples
            for triple in svo_triples:
                subject = triple["subject"]
                predicate = triple["predicate"]
                obj = triple["object"]
                confidence = triple["confidence"]

                # Find corresponding entity IDs
                subject_idx = None
                obj_idx = None

                for i, name in enumerate(entity_names):
                    if name.lower() == subject.lower():
                        subject_idx = i
                    if name.lower() == obj.lower():
                        obj_idx = i

                if (
                    subject_idx is not None
                    and obj_idx is not None
                    and confidence >= 0.4
                ):
                    try:
                        source_event_escaped = self._escape_surrealql(event_id)
                        relate_sql = f"""
                        RELATE {entity_ids[subject_idx]}->fact->{entity_ids[obj_idx]}
                        SET predicate = '{predicate}',
                            source_event = <record>`{source_event_escaped}`,
                            confidence = {confidence:.4f};
                        """
                        relate_result = self._query_surreal(relate_sql)
                        if self._extract_ok(relate_result):
                            facts_created += 1
                            if debug:
                                print(
                                    f"  [KG] SVO Fact: {subject} -[{predicate} ({confidence:.2f})]-> {obj}"
                                )
                    except Exception as e:
                        if debug:
                            print(f"  [KG] Error creating SVO fact: {e}")
        except ImportError:
            # If spaCy is not available, continue with original co-occurrence method
            pass

        # Fall back to co-occurrence method for any remaining entity pairs
        # Uses sentence-level proximity (nicht global O(n²)) + hard cap + Distanz-Confidence
        if len(entity_ids) >= 2:
            # Hard cap: max_entities_for_cooccurrence wählen
            if len(entity_names) > self.config.max_entities_for_cooccurrence:
                selected = self._select_salient_entities(
                    text, entity_names, self.config.max_entities_for_cooccurrence
                )
                name_to_idx = {name: idx for idx, name in enumerate(entity_names)}
                selected_indices = [name_to_idx[name] for name in selected]
                subset_ids = [entity_ids[i] for i in selected_indices]
                subset_names = [entity_names[i] for i in selected_indices]
            else:
                subset_ids = list(entity_ids)
                subset_names = list(entity_names)

            # Satzweise Paarbildung statt globaler Kombinatorik
            sentences = re.split(r"(?<=[.!?])\s+", text)
            paired = set()

            # Embeddings für alle Entities vorberechnen (einmal statt pro Paar)
            entity_embeddings = {}
            for name in subset_names:
                try:
                    entity_embeddings[name] = self.embedding_service.embed_for_storage(name)
                except Exception:
                    pass

            # SVO-Triples einmal vor der Schleife cachen
            svo_triples = None
            try:
                from src.extraction.entity_utils import extract_triples
                svo_triples = extract_triples(text)
            except ImportError:
                pass

            for sentence in sentences:
                sentence_lower = sentence.lower()
                indices_in_sentence = [
                    i
                    for i, name in enumerate(subset_names)
                    if name.lower() in sentence_lower
                ]
                if len(indices_in_sentence) < 2:
                    continue

                for idx_a in indices_in_sentence:
                    for idx_b in indices_in_sentence:
                        if idx_a >= idx_b:
                            continue
                        pair_key = (idx_a, idx_b)
                        if pair_key in paired:
                            continue
                        paired.add(pair_key)

                        # Prüfen ob dieses Paar bereits SVO-Fact hat (nutzt gecachte Triples)
                        has_svo_fact = False
                        if svo_triples:
                            for triple in svo_triples:
                                subj = triple["subject"]
                                obj = triple["object"]
                                if (
                                    subset_names[idx_a].lower() == subj.lower()
                                    and subset_names[idx_b].lower() == obj.lower()
                                ) or (
                                    subset_names[idx_b].lower() == subj.lower()
                                    and subset_names[idx_a].lower() == obj.lower()
                                ):
                                    has_svo_fact = True
                                    break

                        if not has_svo_fact:
                            try:
                                predicate, base_conf = (
                                    self._compute_relation_confidence(
                                        text, subset_names[idx_a], subset_names[idx_b],
                                        emb_a=entity_embeddings.get(subset_names[idx_a]),
                                        emb_b=entity_embeddings.get(subset_names[idx_b]),
                                    )
                                )

                                # Confidence via Embedding-Similarity + textualer Distanz
                                pos_a = sentence_lower.index(
                                    subset_names[idx_a].lower()
                                )
                                pos_b = sentence_lower.index(
                                    subset_names[idx_b].lower()
                                )
                                proximity = 1.0 - min(
                                    abs(pos_a - pos_b) / max(len(sentence), 1), 1.0
                                )
                                confidence = max(
                                    0.1, min(1.0, base_conf * 0.6 + proximity * 0.4)
                                )

                                if confidence >= 0.55:
                                    source_event_escaped = self._escape_surrealql(event_id)
                                    relate_sql = f"""
                                    RELATE {subset_ids[idx_a]}->fact->{subset_ids[idx_b]}
                                    SET predicate = '{predicate}',
                                        source_event = <record>`{source_event_escaped}`,
                                        confidence = {confidence:.4f};
                                    """
                                    relate_result = self._query_surreal(relate_sql)
                                    if self._extract_ok(relate_result):
                                        facts_created += 1
                                        if debug:
                                            print(
                                                f"  [KG] Co-occurrence Fact: {subset_names[idx_a]} -[{predicate} ({confidence:.2f})]-> {subset_names[idx_b]}"
                                            )
                            except Exception as e:
                                if debug:
                                    print(
                                        f"  [KG] Error creating co-occurrence fact: {e}"
                                    )

        for eid in entity_ids:
            try:
                event_escaped = self._escape_surrealql(event_id)
                relate_sql = f"""
                RELATE <record>`{event_escaped}`->fact->{eid}
                SET predicate = 'mentions',
                    confidence = 0.8;
                """
                relate_result = self._query_surreal(relate_sql)
                if self._extract_ok(relate_result):
                    facts_created += 1
            except Exception as e:
                if debug:
                    print(f"  [KG] Error creating mention fact: {e}")

        return {"entities_created": entities_created, "facts_created": facts_created}

    def _dict_to_surrealdb_object(self, d: dict) -> str:
        """Convert a Python dict to a SurrealDB object literal."""
        items = []
        for k, v in d.items():
            key = k
            if v is None:
                val = "NULL"
            elif isinstance(v, bool):
                val = "true" if v else "false"
            elif isinstance(v, (int, float)):
                val = str(v)
            elif isinstance(v, str):
                val = "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
            elif isinstance(v, dict):
                val = self._dict_to_surrealdb_object(v)
            elif isinstance(v, list):
                list_items = []
                for item in v:
                    if isinstance(item, dict):
                        list_items.append(self._dict_to_surrealdb_object(item))
                    elif isinstance(item, str):
                        list_items.append(
                            "'" + item.replace("\\", "\\\\").replace("'", "\\'") + "'"
                        )
                    elif isinstance(item, bool):
                        list_items.append("true" if item else "false")
                    elif item is None:
                        list_items.append("NULL")
                    else:
                        list_items.append(str(item))
                val = "[" + ", ".join(list_items) + "]"
            else:
                val = "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"
            items.append(f"{key}: {val}")
        return "{" + ", ".join(items) + "}"

    def ingest(
        self,
        text: str,
        source: str = "unknown",
        debug: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Hauptfunktion: Ingest eines Textes in das Memory System
        1. IMMER in Raw Event Log speichern
        2. Entropy Gate entscheiden lassen ob KG-Extraction
        3. Bei 'extract': Entities und Facts in den Knowledge Graph extrahieren
        """

        if not text or not text.strip():
            print(f"Error: Empty or whitespace-only content rejected (source={source})")
            return None
        if len(text) > 100_000:
            print(
                f"Error: Content exceeds maximum storage length ({len(text)} > 100000) (source={source})"
            )
            return None
        if "\x00" in text:
            print(f"Error: Content contains null bytes, rejecting (source={source})")
            return None

        # 0. Dedup: Prüfen ob exakt gleicher Content mit gleicher Source bereits existiert
        content_hash = self._hash_content(text)
        source_escaped_dedup = self._escape_surrealql(source)
        dedup_sql = f"""
        SELECT id FROM event
        WHERE content_hash = '{content_hash}'
          AND source = '{source_escaped_dedup}'
          AND (forgotten IS NONE OR forgotten = false)
        LIMIT 1;
        """
        dedup_result = self._query_surreal(dedup_sql)
        existing = self._extract_result(dedup_result)
        if existing and len(existing) > 0:
            event_id = existing[0].get("id")
            if debug:
                print(
                    f"  [Dedup] Found existing event {event_id} for identical content and source"
                )
            gate_result = self.should_extract(text, content_hash=content_hash)
            if gate_result["decision"] == "extract" and event_id:
                kg_result = self._extract_to_kg(text, event_id, debug)
            return event_id

        # 1. IMMER in Raw Event Log speichern (ohne Gate!)
        embedding = self.embedding_service.embed_for_storage(text)
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
        text_escaped = self._escape_surrealql(text)
        source_escaped = self._escape_surrealql(source)
        metadata_str = ""
        if metadata:
            metadata_str = (
                f",\n            metadata = {self._dict_to_surrealdb_object(metadata)}"
            )
        sql = f"""
        CREATE event SET
            content = '{text_escaped}',
            content_hash = '{content_hash}',
            source = '{source_escaped}',
            embedding = {embedding_str}{metadata_str};
        """
        event_id = None
        result = None
        try:
            result = self._query_surreal(sql)
            event_result = self._extract_result(result)
            if event_result and len(event_result) > 0:
                first = event_result[0]
                event_id = first.get("id") if isinstance(first, dict) else None
        except Exception as e:
            import sys

            sys.stderr.write(f"[EntropyGate] Error saving to event log: {e}\n")
            if result:
                sys.stderr.write(f"[EntropyGate]   SurrealDB response: {result}\n")

        if event_id is None:
            import sys

            sys.stderr.write(f"[EntropyGate] WARNING: ingest returned no event_id\n")
            sys.stderr.write(
                f"[EntropyGate]   source={source}, content_length={len(text)}, hash={content_hash[:16]}...\n"
            )

        # 2. Entropy Gate prüfen (mit content_hash, um Self-Match zu vermeiden)
        gate_result = self.should_extract(text, content_hash=content_hash)

        if debug:
            print(f"Entropy Gate Decision: {gate_result}")

        # 3. Falls extract: starte KG-Extraction
        if gate_result["decision"] == "extract" and event_id:
            kg_result = self._extract_to_kg(text, event_id, debug)
            if debug:
                print(f"  [KG] Extraction complete: {kg_result}")

        return event_id

"""
STRATA - LightMem-style Entropy Gate
Composite Score aus Text-Entropy und Embedding-Novelty
Nur vor KG-Write, Raw Event Log bekommt immer alles!
"""
import hashlib
import math
import time
from typing import Optional, Dict, List
import requests
import numpy as np
import json
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import our embedding service
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.extraction.embedding_service import get_embedding_service, BaseEmbeddingService
from src.extraction.entity_utils import infer_entity_type, extract_noun_phrases, is_content_phrase


def escape_surrealql(value: str) -> str:
    """Escape a string for safe use in a SurrealQL string literal."""
    import re
    value = value.replace('\\', '\\\\')
    value = value.replace("'", "\\'")
    value = value.replace('\n', '\\n')
    value = value.replace('\r', '\\r')
    value = value.replace('\t', '\\t')
    value = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', value)
    return value


class EntropyGateConfig:
    def __init__(
        self,
        alpha: float = 0.35,       # Gewicht Text-Entropy
        beta: float = 0.65,        # Gewicht Embedding-Novelty
        threshold: float = 0.55,   # Initialer Default; TODO per gate_log kalibrieren
        min_length: int = 10,      # Unter X Zeichen immer skippen
        max_length: int = 1000     # Über X Zeichen immer skippen
    ):
        self.alpha = alpha
        self.beta = beta
        self.threshold = threshold
        self.min_length = min_length
        self.max_length = max_length


class EntropyGate:
    def __init__(self, embedding_service: Optional[BaseEmbeddingService] = None, config: Optional[EntropyGateConfig] = None):
        self.config = config or EntropyGateConfig()
        self.min_length = self.config.min_length  
        self.max_length = self.config.max_length  
        self.surreal_url = os.getenv("SURREALDB_URL", "http://127.0.0.1:8000/sql")
        self.auth = (os.getenv("SURREALDB_USER", "root"), os.getenv("SURREALDB_PASS", "root"))
        self.surreal_ns = os.getenv("SURREALDB_NS", "strata")  
        self.surreal_db = os.getenv("SURREALDB_DB", "strata")  
        self.embedding_service = embedding_service or get_embedding_service()

    def _query_surreal(self, sql: str) -> List[Dict]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "text/plain; charset=utf-8",
        }
        full_sql = f"USE NS {self.surreal_ns} DB {self.surreal_db};\n{sql}"
        response = requests.post(self.surreal_url, data=full_sql.encode("utf-8"), headers=headers, auth=self.auth, timeout=30)
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

    def calculate_novelty(self, text: str) -> float:
        """
        Novelty based on embedding similarity against existing content in SurrealDB.
        Returns value between 0 (no novelty) and 1 (maximum novelty).
        Uses SurrealDB's native vector functions.
        """
        if not text.strip():
            return 0.0
            
        # Generate embedding for the text
        embedding = self.embedding_service.embed_for_storage(text)
        emb_str = "[" + ", ".join(map(str, embedding)) + "]"
        
        # Search for top-k similar vectors in SurrealDB
        # We use cosine similarity and calculate 1.0 - avg_similarity for novelty
        sql = f"""
        SELECT vector::similarity::cosine(embedding, {emb_str}) AS similarity
        FROM event
        WHERE embedding IS NOT NONE 
          AND array::len(embedding) = {len(embedding)}
          AND (forgotten IS NONE OR forgotten = false)
        ORDER BY similarity DESC
        LIMIT 5;
        """
        
        results = self._query_surreal(sql)
        
        # Extract the results (the _query_surreal here returns [ {status: OK, result: [...]}, ... ])
        actual_results = []
        if results and len(results) > 1: # Index 0 is USE, Index 1 is the query
            actual_results = results[1].get("result", [])
        
        if not actual_results:
            # No similar content found - maximum novelty
            return 1.0
        
        # Calculate average similarity (lower = more novel)
        avg_similarity = sum(float(r["similarity"]) for r in actual_results) / len(actual_results)
        
        # Return inverse (novelty = 1 - similarity)
        return 1.0 - max(0.0, min(1.0, avg_similarity))

    def should_extract(self, text: str) -> Dict[str, any]:
        """
        Entscheidet basierend auf Composite-Score ob Text in KG extrahiert werden soll
        """
        if len(text) < self.config.min_length:
            return {
                "decision": "skip",
                "reason": "text_too_short",
                "text_length": len(text),
                "min_length": self.config.min_length
            }
        if len(text) > self.config.max_length:
            return {
                "decision": "skip",
                "reason": "text_too_long",
                "text_length": len(text),
                "max_length": self.config.max_length
            }
        
        # Calculate individual scores
        text_entropy = self.calculate_char_entropy(text)
        novelty = self.calculate_novelty(text)
        
        # Normalize entropy to 0-1 range (assuming max entropy of ~4.5)
        normalized_entropy = min(text_entropy / 4.5, 1.0)
        
        # Calculate composite score
        composite_score = (self.config.alpha * normalized_entropy) + (self.config.beta * novelty)
        
        # Make decision
        decision = "extract" if composite_score >= self.config.threshold else "ignore"
        
        # Log decision to database
        self._log_decision(text, normalized_entropy, novelty, composite_score, decision)
        
        return {
            "decision": decision,
            "text_entropy": text_entropy,
            "normalized_entropy": normalized_entropy,
            "novelty": novelty,
            "composite_score": composite_score,
            "threshold": self.config.threshold,
            "alpha": self.config.alpha,
            "beta": self.config.beta,
            "reason": f"Composite score {composite_score:.3f} {'meets' if decision == 'extract' else 'does not meet'} threshold {self.config.threshold:.3f}"
        }

    def _log_decision(self, text: str, entropy: float, novelty: float, composite_score: float, decision: str):
        """Log the entropy gate decision to database"""
        try:
            content_hash = self._hash_content(text)
            decision_escaped = self._escape_surrealql(decision)
            sql = f"""
            CREATE gate_log SET 
                content_hash = '{content_hash}',
                text_score = {entropy},
                novelty = {novelty},
                gate_score = {composite_score},
                decision = '{decision_escaped}';
            """
            result = self._query_surreal(sql)
            if not result or len(result) < 2 or result[1].get("status") != "OK":
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
        
        # Try spaCy-based extraction first
        try:
            from src.extraction.entity_utils import extract_entities_with_spacy
            spacy_entities = extract_entities_with_spacy(text)
            candidates = {entity["name"] for entity in spacy_entities}
        except ImportError:
            # If spaCy is not available, fall back to original regex approach
            candidates = set()

        # If spaCy is not available or returns no entities, use original approach as fallback
        if not candidates:
            # 1. Noun-Phrase-Extraktion
            noun_phrases = extract_noun_phrases(text)
            for phrase in noun_phrases:
                candidates.add(phrase)

            # 2. Kleingeschriebene mehrteilige Konzepte (z.B. "quantum computing", "social contract")
            concept_patterns = [
                _re.compile(r'\b[a-z]{3,}(?:\s+[a-z]{3,}){1,2}\b'),
            ]
            for pattern in concept_patterns:
                for match in pattern.finditer(text):
                    candidate = match.group(0).strip()
                    words = candidate.split()
                    if len(words) >= 2 and len(candidate) >= 5:
                        if is_content_phrase(words):
                            candidates.add(candidate)

            # 3. CamelCase-Wörter (z.B. "FastMCP", "SurrealDB")
            for match in _re.finditer(r'\b([A-Z][a-z]+[A-Z][a-zA-Z]*)\b', text):
                candidate = match.group(1).strip()
                if len(candidate) >= 3:
                    candidates.add(candidate)

            # 4. Abkürzungen (z.B. "API", "NER", "KG")
            for match in _re.finditer(r'\b([A-Z]{2,})\b', text):
                candidate = match.group(1).strip()
                if len(candidate) >= 2:
                    candidates.add(candidate)

        return list(candidates)

    def _compute_relation_confidence(self, text: str, entity_a: str, entity_b: str) -> tuple[str, float]:
        """Berechne dynamische Konfidenz und Relationstyp zwischen zwei Entities basierend auf Embedding."""
        emb_service = self.embedding_service
        try:
            emb_a = emb_service.embed_for_storage(entity_a)
            emb_b = emb_service.embed_for_storage(entity_b)
            dot = sum(a * b for a, b in zip(emb_a, emb_b))
            norm = (sum(a * a for a in emb_a) ** 0.5) * (sum(b * b for b in emb_b) ** 0.5)
            similarity = dot / norm if norm > 0 else 0.0
            confidence = max(0.0, min(1.0, similarity))
            label = self._infer_relation_type(text, entity_a, entity_b, confidence)
            return label, confidence
        except Exception:
            return "co_occurs_with", 0.5

    def _infer_relation_type(self, text: str, entity_a: str, entity_b: str, confidence: float) -> str:
        """Determine the relationship type between two entities."""
        lower_text = text.lower()
        a_lower = entity_a.lower()
        b_lower = entity_b.lower()

        works_verbs = ['works at', 'works for', 'employed by', 'joined', 'led by']
        for verb in works_verbs:
            if verb in lower_text:
                if a_lower in lower_text.split(verb)[0] if verb in lower_text else False:
                    return "works_at"

        located_verbs = ['located in', 'based in', 'situated in']
        for verb in located_verbs:
            if verb in lower_text:
                return "located_in"

        created_verbs = ['created', 'developed', 'built', 'founded', 'implemented']
        for verb in created_verbs:
            if verb in lower_text:
                return "created"

        if confidence > 0.85:
            return "strongly_related"
        elif confidence > 0.7:
            return "related_to"
        elif confidence > 0.5:
            return "co_occurs_with"
        else:
            return "weakly_related"

    def _ensure_entity(self, name: str) -> Optional[str]:
        """Create an entity if it does not exist. Returns the entity ID."""
        name_escaped = self._escape_surrealql(name)
        entity_type = infer_entity_type(name, self.embedding_service)
        
        # 1. Exact name match (fastest path)
        check_sql = f"SELECT id FROM entity WHERE name = '{name_escaped}' LIMIT 1;"
        check_result = self._query_surreal(check_sql)
        if check_result and len(check_result) > 1:
            existing = check_result[1].get("result", [])
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
                embedding = {embedding_str};
            """
        except Exception:
            # If embedding fails, create entity without embedding
            create_sql = f"""
            CREATE entity SET 
                name = '{name_escaped}',
                type = '{entity_type}';
            """
        
        result = self._query_surreal(create_sql)
        if result and len(result) > 1:
            entity_result = result[1].get("result", [])
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
            if result and len(result) > 1:
                entities = result[1].get("result", [])
                if entities and len(entities) > 0:
                    return entities[0].get("id")
        except Exception as e:
            # If embedding fails, skip similarity search
            pass
        return None

    def _extract_to_kg(self, text: str, event_id: str, debug: bool = False):
        """
        Extract entities and semantic relationships to the Knowledge Graph.
        Uses SVO extraction as primary method, with co-occurrence as fallback.
        """
        # First, try SVO extraction if spaCy is available
        try:
            from src.extraction.entity_utils import extract_triples, extract_entities_with_spacy
            svo_triples = extract_triples(text)
            spacy_entities = extract_entities_with_spacy(text)
            
            # Extract entities from SVO triples if any
            svo_entities = set()
            for triple in svo_triples:
                svo_entities.add(triple["subject"])
                svo_entities.add(triple["object"])
            
            # Combine entities from SVO and spaCy NER
            all_candidates = set()
            for entity in spacy_entities:
                all_candidates.add(entity["name"])
            all_candidates.update(svo_entities)
            
            candidates = list(all_candidates)
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
            eid = self._ensure_entity(name)
            if eid:
                entity_ids.append(eid)
                entity_names.append(name)
                entities_created += 1
                if debug:
                    print(f"  [KG] Entity: {name} -> {eid}")

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
                
                if subject_idx is not None and obj_idx is not None and confidence >= 0.3:
                    try:
                        relate_sql = f"""
                        RELATE {entity_ids[subject_idx]}->fact->{entity_ids[obj_idx]} 
                        SET predicate = '{predicate}',
                            source_event = {event_id},
                            confidence = {confidence:.4f};
                        """
                        relate_result = self._query_surreal(relate_sql)
                        if relate_result and len(relate_result) > 1 and relate_result[1].get("status") == "OK":
                            facts_created += 1
                            if debug:
                                print(f"  [KG] SVO Fact: {subject} -[{predicate} ({confidence:.2f})]-> {obj}")
                    except Exception as e:
                        if debug:
                            print(f"  [KG] Error creating SVO fact: {e}")
        except ImportError:
            # If spaCy is not available, continue with original co-occurrence method
            pass

        # Fall back to co-occurrence method for any remaining entity pairs
        if len(entity_ids) >= 2:
            for i in range(len(entity_ids)):
                for j in range(i + 1, len(entity_ids)):
                    # Check if this pair already has a fact from SVO extraction
                    has_svo_fact = False
                    try:
                        from src.extraction.entity_utils import extract_triples
                        svo_triples = extract_triples(text)
                        for triple in svo_triples:
                            subject = triple["subject"]
                            obj = triple["object"]
                            
                            if (entity_names[i].lower() == subject.lower() and entity_names[j].lower() == obj.lower()) or \
                               (entity_names[j].lower() == subject.lower() and entity_names[i].lower() == obj.lower()):
                                has_svo_fact = True
                                break
                    except ImportError:
                        # If spaCy not available, process all pairs
                        pass
                    
                    if not has_svo_fact:
                        try:
                            predicate, confidence = self._compute_relation_confidence(
                                text, entity_names[i], entity_names[j]
                            )
                            if confidence >= 0.3:
                                relate_sql = f"""
                                RELATE {entity_ids[i]}->fact->{entity_ids[j]} 
                                SET predicate = '{predicate}',
                                    source_event = {event_id},
                                    confidence = {confidence:.4f};
                                """
                                relate_result = self._query_surreal(relate_sql)
                                if relate_result and len(relate_result) > 1 and relate_result[1].get("status") == "OK":
                                    facts_created += 1
                                    if debug:
                                        print(f"  [KG] Co-occurrence Fact: {entity_names[i]} -[{predicate} ({confidence:.2f})]-> {entity_names[j]}")
                        except Exception as e:
                            if debug:
                                print(f"  [KG] Error creating co-occurrence fact: {e}")

        for eid in entity_ids:
            try:
                relate_sql = f"""
                RELATE {event_id}->fact->{eid} 
                SET predicate = 'mentions',
                    confidence = 0.8;
                """
                relate_result = self._query_surreal(relate_sql)
                if relate_result and len(relate_result) > 1 and relate_result[1].get("status") == "OK":
                    facts_created += 1
            except Exception as e:
                if debug:
                    print(f"  [KG] Error creating mention fact: {e}")

        return {"entities_created": entities_created, "facts_created": facts_created}
    
    def ingest(self, text: str, source: str = "unknown", debug: bool = False) -> Optional[str]:
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
            print(f"Error: Content exceeds maximum storage length ({len(text)} > 100000) (source={source})")
            return None
        if '\x00' in text:
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
        if dedup_result and len(dedup_result) > 1:
            existing = dedup_result[1].get("result", [])
            if existing and len(existing) > 0:
                event_id = existing[0].get("id")
                if debug:
                    print(f"  [Dedup] Found existing event {event_id} for identical content and source")
                gate_result = self.should_extract(text)
                if gate_result["decision"] == "extract" and event_id:
                    kg_result = self._extract_to_kg(text, event_id, debug)
                return event_id

        # 1. IMMER in Raw Event Log speichern (ohne Gate!)
        embedding = self.embedding_service.embed_for_storage(text)
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
        text_escaped = self._escape_surrealql(text)
        source_escaped = self._escape_surrealql(source)
        sql = f"""
        CREATE event SET 
            content = '{text_escaped}',
            content_hash = '{content_hash}',
            source = '{source_escaped}',
            embedding = {embedding_str};
        """
        event_id = None
        result = None
        try:
            result = self._query_surreal(sql)
            if result and len(result) > 1:
                event_result = result[1].get("result", [])
                if event_result and isinstance(event_result, list) and len(event_result) > 0:
                    event_id = event_result[0].get("id")
        except Exception as e:
            import sys
            sys.stderr.write(f"[EntropyGate] Error saving to event log: {e}\n")
            if result:
                sys.stderr.write(f"[EntropyGate]   SurrealDB response: {result}\n")
        
        if event_id is None:
            import sys
            sys.stderr.write(f"[EntropyGate] WARNING: ingest returned no event_id\n")
            sys.stderr.write(f"[EntropyGate]   source={source}, content_length={len(text)}, hash={content_hash[:16]}...\n")
        
        # 2. Entropy Gate prüfen
        gate_result = self.should_extract(text)
        
        if debug:
            print(f"Entropy Gate Decision: {gate_result}")
        
        # 3. Falls extract: starte KG-Extraction
        if gate_result["decision"] == "extract" and event_id:
            kg_result = self._extract_to_kg(text, event_id, debug)
            if debug:
                print(f"  [KG] Extraction complete: {kg_result}")
        
        return event_id

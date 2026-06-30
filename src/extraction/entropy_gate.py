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
            "Content-Type": "text/plain",
        }
        full_sql = f"USE NS {self.surreal_ns} DB {self.surreal_db};\n{sql}"
        response = requests.post(self.surreal_url, data=full_sql, headers=headers, auth=self.auth, timeout=30)
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

    def _hash_content(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def calculate_char_entropy(self, text: str) -> float:
        """
        Shannon-Entropy auf Zeichenebene (wie LightMem)
        Gibt *unnormalisierte* Entropy zurück (typisch 0-4.5)
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
        Novelty basierend auf Embedding-Ähnlichkeit mit bisherigen Inhalten in SurrealDB.
        Gibt Wert zwischen 0 (keine novelty) und 1 (maximale novelty).
        Nutzt SurrealDB's native Vektor-Funktionen.
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
            sql = f"""
            CREATE gate_log SET 
                content_hash = '{content_hash}',
                text_score = {entropy},
                novelty = {novelty},
                gate_score = {composite_score},
                decision = '{decision}';
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
        Extrahiert Kandidaten-Entities aus dem Text.
        Sucht nach großgeschriebenen Wörtern/Phrasen (einfache NER-Heuristik).
        """
        import re
        # Finde großgeschriebene Wörter (potentielle Eigennamen)
        candidates = set()
        
        # Pattern: Aufeinanderfolgende großgeschriebene Wörter (z.B. "SurrealDB", "Entropy Gate")
        for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', text):
            candidate = match.group(1).strip()
            if len(candidate) >= 3:  # Mindestens 3 Zeichen
                candidates.add(candidate)
        
        # Pattern: CamelCase-Wörter (z.B. "EntropyGate", "FastMCP")
        for match in re.finditer(r'\b([A-Z][a-z]+[A-Z][a-zA-Z]*)\b', text):
            candidate = match.group(1).strip()
            if len(candidate) >= 3:
                candidates.add(candidate)
        
        # Pattern: Abkürzungen (z.B. "ATP", "CRISPR", "DNA")
        for match in re.finditer(r'\b([A-Z]{2,})\b', text):
            candidate = match.group(1).strip()
            if len(candidate) >= 2:
                candidates.add(candidate)
        
        return list(candidates)

    def _infer_entity_type(self, name: str) -> str:
        """Bestimmt den Entity-Typ basierend auf dem Namen."""
        lower = name.lower()
        if any(suffix in lower for suffix in ['corp', 'inc', 'ltd', 'gmbh', 'company', 'org', 'ag']):
            return 'organization'
        if any(suffix in lower for suffix in ['gate', 'system', 'framework', 'engine', 'server']):
            return 'technology'
        if any(suffix in lower for suffix in ['theorie', 'effekt', 'mechanik', 'technologie']):
            return 'concept'
        return 'concept'

    def _ensure_entity(self, name: str) -> Optional[str]:
        """Legt eine Entity an, falls sie noch nicht existiert. Gibt die ID zurück."""
        name_escaped = name.replace("'", "\\'")
        entity_type = self._infer_entity_type(name)
        
        # Prüfen ob Entity bereits existiert
        check_sql = f"SELECT id FROM entity WHERE name = '{name_escaped}' LIMIT 1;"
        check_result = self._query_surreal(check_sql)
        if check_result and len(check_result) > 1:
            existing = check_result[1].get("result", [])
            if existing and len(existing) > 0:
                return existing[0].get("id")
        
        # Neue Entity anlegen
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

    def _extract_to_kg(self, text: str, event_id: str, debug: bool = False):
        """
        Extrahiert Entities und Facts aus dem Text in den Knowledge Graph.
        """
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
        
        # Entities anlegen
        for name in candidates:
            eid = self._ensure_entity(name)
            if eid:
                entity_ids.append(eid)
                entities_created += 1
                if debug:
                    print(f"  [KG] Entity: {name} -> {eid}")
        
        # Facts zwischen Entities und Event anlegen (co-occurrence)
        if len(entity_ids) >= 2:
            for i in range(len(entity_ids)):
                for j in range(i + 1, len(entity_ids)):
                    try:
                        relate_sql = f"""
                        RELATE {entity_ids[i]}->fact->{entity_ids[j]} 
                        SET predicate = 'co_occurs_with',
                            source_event = {event_id},
                            confidence = 0.5;
                        """
                        relate_result = self._query_surreal(relate_sql)
                        if relate_result and len(relate_result) > 1 and relate_result[1].get("status") == "OK":
                            facts_created += 1
                    except Exception as e:
                        if debug:
                            print(f"  [KG] Error creating fact: {e}")
        
        # Fact: Event -> mentioned_in -> Entity (für jede Entity)
        for eid in entity_ids:
            try:
                # Verwende das Event als Subject und Entity als Object
                # SurrealDB: event->fact->entity mit predicate 'mentions'
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
        
        # 1. IMMER in Raw Event Log speichern (ohne Gate!)
        embedding = self.embedding_service.embed_for_storage(text)
        text_escaped = text.replace("'", "\\'")
        sql = f"""
        CREATE event SET 
            content = '{text_escaped}',
            source = '{source}',
            embedding = {embedding};
        """
        try:
            result = self._query_surreal(sql)
            if result and len(result) > 1:
                event_result = result[1].get("result", [])
                if event_result and isinstance(event_result, list) and len(event_result) > 0:
                    event_id = event_result[0].get("id")
                else:
                    event_id = None
            else:
                event_id = None
        except Exception as e:
            print(f"Error saving to event log: {e}")
            event_id = None
        
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

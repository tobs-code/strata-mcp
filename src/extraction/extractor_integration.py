"""
Extractor Integration for Strata
Coordinates the integration between extraction components and the broader system
"""
from typing import Dict, Any, Optional
import sys
import os
import httpx
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.extraction.coarse_extractor import ExtractionPipeline
from src.extraction.entropy_gate import EntropyGate, escape_surrealql
from src.extraction.embedding_service import get_embedding_service


SURREAL_URL = os.getenv("SURREALDB_URL", "http://127.0.0.1:8000/sql")
SURREAL_AUTH = (os.getenv("SURREALDB_USER", "root"), os.getenv("SURREALDB_PASS", "root"))
SURREAL_NS = os.getenv("SURREALDB_NS", "strata")
SURREAL_DB = os.getenv("SURREALDB_DB", "strata")


async def _query_surreal(sql: str) -> Any:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    full_sql = f"USE NS {SURREAL_NS} DB {SURREAL_DB};\n{sql}"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            SURREAL_URL,
            content=full_sql,
            headers=headers,
            auth=SURREAL_AUTH,
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("status") == "ERR":
                    raise RuntimeError(
                        f"SurrealDB Error: {item.get('information') or item.get('result')} | SQL: {sql[:120]}"
                    )
        return data


def _extract_surreal_result(data: Any) -> list:
    if not isinstance(data, list):
        return []
    candidates = [
        item
        for item in data
        if isinstance(item, dict)
        and item.get("status") == "OK"
        and "result" in item
    ]
    if not candidates:
        return []
    result = candidates[-1].get("result", [])
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return []


class ExtractorIntegration:
    """Main integration point for extraction components"""

    def __init__(self, entropy_gate: Optional[EntropyGate] = None):
        self.pipeline = ExtractionPipeline()
        self.entropy_gate = entropy_gate or EntropyGate()
        self.embedding_service = get_embedding_service()

    def process_text(self, text: str, source: str = "unknown") -> Dict[str, Any]:
        """
        Process text through the full extraction pipeline.
        Applies entropy filtering to determine if extraction should proceed.
        """
        entropy_result = self.entropy_gate.should_extract(text)

        result = {
            "text": text,
            "source": source,
            "entropy_gate_result": entropy_result,
            "extraction_result": None,
            "applied_extraction": False,
        }

        if entropy_result["decision"] == "extract":
            extraction_result = self.pipeline.process(text, apply_entropy_filter=False)
            result["extraction_result"] = extraction_result
            result["applied_extraction"] = True

            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                asyncio.create_task(self._store_extracted_entities(extraction_result, source))
            else:
                asyncio.run(self._store_extracted_entities(extraction_result, source))

        return result

    async def _store_extracted_entities(
        self, extraction_result: Dict[str, Any], source: str = "extraction"
    ) -> Dict[str, Any]:
        """
        Persist extracted entities and relations into SurrealDB.
        Returns a summary dict of what was stored.
        """
        entities = extraction_result.get("entities", {})
        relations = extraction_result.get("relations", [])

        entity_type_mapping = {
            "person": "person",
            "organization": "organization",
            "location": "location",
            "date": "date",
            "email": "contact",
            "phone": "contact",
            "url": "url",
        }

        entity_id_map: Dict[str, str] = {}

        stored_entities = 0
        stored_facts = 0

        for entity_type, names in entities.items():
            mapped_type = entity_type_mapping.get(entity_type, entity_type)
            for name in names:
                name_escaped = escape_surrealql(name)
                lookup_sql = (
                    f"SELECT id FROM entity WHERE name = '{name_escaped}' LIMIT 1;"
                )
                lookup_result = await _query_surreal(lookup_sql)
                found = _extract_surreal_result(lookup_result)

                if found:
                    entity_id = found[0]["id"]
                else:
                    create_sql = (
                        f"CREATE entity SET name = '{name_escaped}', "
                        f"type = '{mapped_type}';"
                    )
                    create_result = await _query_surreal(create_sql)
                    created = _extract_surreal_result(create_result)
                    entity_id = created[0]["id"] if created else None

                if entity_id:
                    entity_id_map[f"{entity_type}:{name}"] = entity_id
                    stored_entities += 1

        for relation in relations:
            verbs = relation.get("verbs", [])
            predicate = verbs[0] if verbs else "related_to"
            entity_entries = list(relation.get("entities", {}).items())
            if len(entity_entries) < 2:
                continue

            subject_name = entity_entries[0][1][0] if entity_entries[0][1] else None
            object_name = entity_entries[1][1][0] if len(entity_entries) > 1 and entity_entries[1][1] else None

            if not subject_name or not object_name:
                continue

            subject_id = entity_id_map.get(f"{entity_entries[0][0]}:{subject_name}")
            object_id = entity_id_map.get(f"{entity_entries[1][0]}:{object_name}")

            if not subject_id or not object_id or subject_id == object_id:
                continue

            subject_escaped = escape_surrealql(subject_name)
            predicate_escaped = escape_surrealql(predicate)
            object_escaped = escape_surrealql(object_name)

            sql = (
                f"SELECT id FROM fact "
                f"WHERE in = '{subject_id}' "
                f"AND out = '{object_id}' "
                f"AND predicate = '{predicate_escaped}' "
                f"AND valid_until = NONE "
                f"LIMIT 1;"
            )

            existing = await _query_surreal(sql)
            if _extract_surreal_result(existing):
                continue

            relate_sql = (
                f"RELATE '{subject_id}'->fact->'{object_id}' "
                f"SET predicate = '{predicate_escaped}', "
                f"confidence = 1.0, "
                f"source = '{source}';"
            )

            try:
                await _query_surreal(relate_sql)
                stored_facts += 1
            except Exception as e:
                print(f"Failed to store fact {subject_name} {predicate} {object_name}: {e}")

        return {
            "stored_entities": stored_entities,
            "stored_facts": stored_facts,
        }


# Example usage
if __name__ == "__main__":
    # Initialize the integrated extractor
    extractor = ExtractorIntegration()
    
    # Test with various types of content
    test_texts = [
        "John Smith works at Acme Corp in New York.",
        "The weather is nice today.",
        "Meeting scheduled for tomorrow at 3 PM with Alice Johnson.",
        "The quarterly report shows increased revenue.",
        "Short text."  # This might be filtered by entropy gate
    ]
    
    for i, text in enumerate(test_texts):
        print(f"\n--- Processing text {i+1} ---")
        result = extractor.process_text(text, f"test_source_{i+1}")
        
        print(f"Text: {text}")
        print(f"Entropy gate decision: {result['entropy_gate_result']['decision']}")
        print(f"Applied extraction: {result['applied_extraction']}")
        
        if result['applied_extraction']:
            extraction = result['extraction_result']
            print(f"Found {extraction['entity_count']} entities and {extraction['relation_count']} relations")
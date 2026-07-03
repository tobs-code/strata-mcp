"""
Coarse Extractor for sieveon
Schema-free extraction with regex-based entity recognition
Avoids overly aggressive parsing in favor of conservative extraction
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple


class CoarseExtractor:
    def __init__(self):
        # Simple regex patterns for coarse entity recognition
        self.patterns = {
            "person": r"\b(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b",
            "organization": r"\b[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)*(?:\s+(?:Inc|Corp|Ltd|GmbH|AG|LLC))\b",
            "location": r"\b[A-Z][a-z]+,\s*[A-Z]{2}\b|\b[A-Z][a-z]+\b",
            "date": r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
            "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
            "phone": r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",
            "url": r"https?://(?:[-\w.])+(?:\:[0-9]+)?(?:/(?:[\w/_.])*(?:\?(?:[\w&=%.])*)?(?:\#(?:[\w.])*)?)?",
        }

    def extract(self, text: str) -> Dict[str, List[str]]:
        """
        Perform coarse extraction of entities from text
        Returns dictionary with entity type as key and list of matches as value
        """
        results = {}

        for entity_type, pattern in self.patterns.items():
            matches = re.findall(pattern, text)
            if matches:
                results[entity_type] = matches

        return results

    def extract_relations(self, text: str) -> List[Dict[str, str]]:
        """
        Extract potential subject-predicate-object relations from text
        Uses simple heuristics rather than complex NLP
        """
        relations = []

        # Simple pattern: [Subject] [verb] [object]
        # This is intentionally coarse to avoid over-parsing
        sentences = re.split(r"[.!?]+", text)

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            # Extract entities in this sentence
            entities = self.extract(sentence)

            # Look for simple verb patterns between entities
            words = sentence.lower().split()
            verbs = [
                w
                for w in words
                if w
                in [
                    "is",
                    "are",
                    "was",
                    "were",
                    "has",
                    "have",
                    "had",
                    "works",
                    "worked",
                    "works",
                    "lives",
                    "lived",
                    "met",
                    "met",
                ]
            ]

            if len(entities) >= 2 and verbs:
                # Create a simple relation - this is very coarse
                relation = {"sentence": sentence, "verbs": verbs, "entities": entities}
                relations.append(relation)

        return relations


class ExtractionPipeline:
    """Pipeline for coordinating extraction operations"""

    def __init__(self):
        self.extractor = CoarseExtractor()

    def process(self, text: str, apply_entropy_filter: bool = True) -> Dict[str, Any]:
        """
        Process text through the extraction pipeline
        Optionally applies entropy filtering to decide if extraction is worthwhile
        """
        # Perform coarse extraction
        entities = self.extractor.extract(text)
        relations = self.extractor.extract_relations(text)

        result = {
            "original_text": text,
            "entities": entities,
            "relations": relations,
            "entity_count": sum(len(matches) for matches in entities.values()),
            "relation_count": len(relations),
        }

        return result


# Example usage
if __name__ == "__main__":
    extractor = CoarseExtractor()

    sample_text = """
    John Smith works at Acme Corp. He met with Jane Doe from Tech Inc.
    The meeting took place on March 15, 2024 at 2:30 PM.
    Contact John at john.smith@acme.com or call (555) 123-4567.
    The office is located in New York, NY.
    """

    entities = extractor.extract(sample_text)
    relations = extractor.extract_relations(sample_text)

    print("Entities found:")
    for entity_type, matches in entities.items():
        print(f"  {entity_type}: {matches}")

    print(f"\nRelations found: {len(relations)}")
    for rel in relations:
        print(f"  {rel}")

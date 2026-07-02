"""Shared entity utilities for STRATA - single source of truth."""
from typing import Optional
import json
import os
import re
import requests
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.extraction.embedding_service import get_embedding_service, BaseEmbeddingService

# Global variable for spaCy model (lazy-loaded)
_nlp = None

def _get_nlp():
    """Lazy-load spaCy model with fallback to regex if not available."""
    global _nlp
    if _nlp is None:
        try:
            import spacy
            _nlp = spacy.load("en_core_web_sm")
        except (ImportError, OSError):
            # spaCy not installed or model not available
            _nlp = None
    return _nlp

_STOPWORDS = {
    'the', 'a', 'an', 'this', 'that', 'these', 'those', 'it', 'its',
    'in', 'on', 'at', 'by', 'for', 'with', 'from', 'to', 'of', 'and', 'or',
    'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
    'do', 'does', 'did', 'will', 'would', 'shall', 'should', 'may', 'might',
    'can', 'could', 'must', 'not', 'no', 'nor', 'but', 'if', 'as', 'so',
    'this', 'that', 'there', 'their', 'them', 'they', 'then', 'than',
    'also', 'very', 'just', 'like', 'into', 'about', 'over', 'such',
    'each', 'which', 'what', 'who', 'whom', 'when', 'where', 'why',
    'how', 'all', 'both', 'every', 'some', 'any', 'few', 'more', 'most',
    'other', 'another',
}

_PREPOSITION_STARTS = {
    'in', 'on', 'at', 'by', 'for', 'with', 'from', 'to', 'of', 'about',
    'over', 'under', 'through', 'between', 'among', 'against', 'without',
    'during', 'before', 'after', 'above', 'below', 'out', 'off', 'up',
    'down', 'into', 'onto', 'upon', 'within', 'across', 'along', 'around',
    'behind', 'beneath', 'beside', 'beyond', 'inside', 'outside', 'toward',
    'towards', 'via', 'per',
}

_VERB_STARTS = {
    'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
    'do', 'does', 'did', 'will', 'would', 'shall', 'should', 'may', 'might',
    'can', 'could', 'must',
    'works', 'worked', 'working', 'relies', 'relied', 'relying', 'based',
    'lives', 'lived', 'living', 'says', 'said', 'made', 'makes', 'making',
    'uses', 'used', 'using', 'takes', 'took', 'taking', 'gives', 'gave',
}

_ENTITY_PROTOTYPES = {
    "organization": [
        "Acme Corp", "Microsoft Inc", "Google LLC", "OpenAI Ltd",
        "Red Cross", "United Nations", "Stanford University",
    ],
    "technology": [
        "SQL Database", "Web Framework", "API Gateway", "MCP Server", "Entropy Gate",
        "Protocol", "Engine", "Platform", "Toolkit", "Runtime",
    ],
    "person": [
        "John Smith", "Alice Johnson", "Jane Doe", "Mr Bond",
        "Dr Watson", "Prof Higgins",
    ],
    "concept": [
        "The Theory of Everything", "Quantum Mechanics", "Social Contract",
        "Cognitive Bias", "Paradigm Shift", "Heuristics", "Algorithm",
    ],
}

# Ontology definition for Phase 4
ONTOLOGY = {
    "entity_types": [
        "person", "organization", "location", "technology", "concept", "event",
    ],
    "predicate_types": {
        "works_at":        {"source": "person",       "target": "organization"},
        "located_in":      {"source": "organization", "target": "location"},
        "developed":       {"source": "person",       "target": ["technology", "concept"]},
        "founded":         {"source": "person",       "target": "organization"},
        "uses":            {"source": ["person", "organization"], "target": ["technology", "concept"]},
        "part_of":         {"source": "concept",      "target": "concept"},
        "leads":           {"source": "person",       "target": ["organization", "project"]},
        "wrote":           {"source": "person",       "target": "concept"},
        "published":       {"source": "person",       "target": "concept"},
        "related_to":      {"source": "*",            "target": "*"},  # Catch-all
    },
}


def validate_predicate(subject_type: str, predicate: str, object_type: str) -> bool:
    """Checks if the relation is allowed according to the ontology.
    On violation: fallback to 'related_to' (no exception thrown)."""
    spec = ONTOLOGY["predicate_types"].get(predicate)
    if spec is None:
        return False
    if spec["source"] != "*" and subject_type not in (
        spec["source"] if isinstance(spec["source"], list) else [spec["source"]]
    ):
        return False
    if spec["target"] != "*" and object_type not in (
        spec["target"] if isinstance(spec["target"], list) else [spec["target"]]
    ):
        return False
    return True


def _get_prototype_embedding(emb_service: BaseEmbeddingService, texts: list[str]) -> list[float]:
    key = "||".join(sorted(texts))
    if key in _EMBEDDING_CACHE:
        return _EMBEDDING_CACHE[key]
    if not texts:
        result = [0.0] * 768
        _EMBEDDING_CACHE[key] = result
        return result
    all_embs = [emb_service.embed_for_storage(t) for t in texts]
    avg = [sum(vals) / len(vals) for vals in zip(*all_embs)]
    _EMBEDDING_CACHE[key] = avg
    return avg


def is_content_phrase(words: list[str]) -> bool:
    """Check if a phrase contains at least one content word (not all stopwords/prepositions/verbs)."""
    if not words:
        return False
    content_count = 0
    for w in words:
        wl = w.lower().strip('.,;:!?()[]{}""''')
        if not wl:
            continue
        if wl not in _STOPWORDS:
            content_count += 1
        if w[0].isupper() and wl not in _STOPWORDS:
            content_count += 2
    if content_count == 0:
        return False
    first_word = words[0].lower().strip('.,;:!?()[]{}""''')
    if first_word in _PREPOSITION_STARTS or first_word in _VERB_STARTS:
        if content_count <= 1:
            return False
    return True


def extract_entities_with_spacy(text: str) -> list[dict]:
    """Extract entities using spaCy NER as primary source, with regex fallback."""
    nlp = _get_nlp()
    
    if nlp is not None:
        # Use spaCy as primary source
        doc = nlp(text)
        entities = []
        
        # Get named entities from spaCy
        for ent in doc.ents:
            entity_type = map_spacy_label_to_strata(ent.label_)
            entities.append({
                "name": ent.text,
                "type": entity_type,
                "label": ent.label_,
                "confidence": 0.99  # High confidence for spaCy entities
            })
        
        # Also get noun chunks as additional candidates
        for chunk in doc.noun_chunks:
            # Skip if already captured as entity
            if not any(chunk.text.lower() == ent["name"].lower() for ent in entities):
                # Infer type based on capitalization and context
                entity_type = infer_entity_type(chunk.text)
                entities.append({
                    "name": chunk.text,
                    "type": entity_type,
                    "label": "NOUN_CHUNK",
                    "confidence": 0.7  # Medium confidence for noun chunks
                })
        
        return entities
    else:
        # Fallback to regex-based extraction
        regex_entities = []
        noun_phrases = extract_noun_phrases(text)
        for phrase in noun_phrases:
            entity_type = infer_entity_type(phrase)
            regex_entities.append({
                "name": phrase,
                "type": entity_type,
                "label": "REGEX",
                "confidence": 0.5  # Lower confidence for regex
            })
        
        return regex_entities


_GROQ_API_KEY = None

def _get_groq_key() -> str | None:
    global _GROQ_API_KEY
    if _GROQ_API_KEY is None:
        _GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    return _GROQ_API_KEY if _GROQ_API_KEY else None

_GROQ_SYSTEM_PROMPT = (
    "You are a precise entity extraction system. Identify every specific named entity in the text.\n\n"
    "OUTPUT FORMAT (one per line): name | type | confidence\n\n"
    "Valid types: person, organization, location, technology, concept, event\n\n"
    "RULES:\n"
    "1. Extract specific named entities: people, companies, products, places, benchmarks, laws/acts, dates\n"
    "2. Skip generic terms: job roles, common nouns, measurements, prices, standalone years, single generic words\n"
    "3. Universities, institutes, and colleges are organizations, not locations\n"
    "4. Generic event descriptions like 'conference' or 'meeting' without a proper name should be skipped\n"
    "5. Confidence: 0.90-0.99 for clear proper names, 0.70-0.89 when ambiguous or partial\n"
    "6. Output ONLY the pipe lines — no greetings, no explanations, no markdown"
)

_GROQ_FEW_SHOT_EXAMPLES = """\
Text: Google released Gemini 2.0 and Microsoft launched Copilot.
Output:
Google | organization | 0.99
Gemini 2.0 | technology | 0.95
Microsoft | organization | 0.99
Copilot | technology | 0.90

Text: Anthropic's Claude 3.5 Sonnet scored 88% on SWE-bench.
Output:
Anthropic | organization | 0.99
Claude 3.5 Sonnet | technology | 0.99
SWE-bench | technology | 0.95"""


def extract_entities_with_groq(text: str) -> list[dict]:
    key = _get_groq_key()
    if not key:
        return []
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": _GROQ_SYSTEM_PROMPT},
            {"role": "user", "content": f"Examples:\n{_GROQ_FEW_SHOT_EXAMPLES}\n\nText: {text}\nOutput:"},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
        "stop": ["\n\n"],
    }
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return []

    entities = []
    seen = set()
    for line in content.split("\n"):
        line = line.strip()
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or len(parts[0]) < 2:
            continue
        name = parts[0]
        etype = parts[1].lower()
        if etype not in {"person", "organization", "location", "technology", "concept", "event"}:
            etype = "concept"
        # Universities/institutes are organizations, not locations
        if etype == "location":
            lower = name.lower()
            if any(kw in lower for kw in ("university", "institute of technology", "college", "school of", "institut")):
                etype = "organization"
        conf = 0.7
        if len(parts) >= 3:
            try:
                conf = min(1.0, max(0.0, float(parts[2])))
            except ValueError:
                pass
        key = f"{name.lower()}|{etype}"
        if key in seen:
            continue
        seen.add(key)
        entities.append({"name": name, "type": etype, "label": "GROQ", "confidence": conf})
    return entities


def extract_entities(text: str) -> list[dict]:
    method = os.getenv("EXTRACTION_METHOD", "auto")
    if method == "groq":
        entities = extract_entities_with_groq(text)
        if entities:
            return entities
    if method in ("groq", "auto"):
        entities = extract_entities_with_groq(text)
        if entities:
            return entities
    return extract_entities_with_spacy(text)


def map_spacy_label_to_strata(spacy_label: str) -> str:
    """Map spaCy labels to STRATA entity types."""
    label_mapping = {
        "PERSON": "person",
        "ORG": "organization", 
        "GPE": "location",  # Geopolitical entity (countries, cities, states)
        "LOC": "location",
        "PRODUCT": "technology",
        "EVENT": "event",
        "WORK_OF_ART": "concept",
        "NORP": "concept",  # Nationalities or religious or political groups
        "FAC": "location",  # Facilities
    }
    return label_mapping.get(spacy_label, "concept")


def infer_entity_type(name: str, embedding_service: Optional[BaseEmbeddingService] = None) -> str:
    """Infer entity type using suffix heuristics (fast path) + embedding similarity (slow path)."""
    lower = name.lower().strip()

    if not lower:
        return "concept"

    if any(suffix in lower for suffix in ['corp', 'inc', 'ltd', 'ltd', 'company', 'org', 'ag', 'llc', 'corp.', 'inc.', 'ltd.', '& co', 'e.l.l.c.']):
        return 'organization'
    if any(suffix in lower for suffix in ['gate', 'system', 'framework', 'engine', 'server', 'protocol', 'database', 'platform', 'service', 'tool', 'api', 'sdk', 'runtime', 'client', 'agent', 'model', 'code', 'cli', 'studio', 'os', 'suite', 'app', 'bot', 'flash', 'nano', 'codex', 'inference', 'benchmark', 'infer', 'train', 'dataset', 'embedding', 'vector', 'reasoning', 'token', 'layer', 'params', 'neural', 'transformer', 'attention', 'encoder', 'decoder', 'quantum', 'blockchain']):
        return 'technology'
    if any(suffix in lower for suffix in ['theory', 'effect', 'mechanics', 'technology', 'principle', 'rule', 'law', 'theorem', 'axiom', 'paradigm', 'method', 'algorithm', 'concept']):
        return 'concept'
    if any(suffix in lower for suffix in ['street', 'place', 'avenue', 'lane', 'road', 'way', 'boulevard']):
        return 'location'
    if any(prefix in lower for prefix in ['dr ', 'mr ', 'ms ', 'mrs ', 'prof ', 'miss ', 'sir ', 'lord ', 'lady ']):
        return 'person'

    if embedding_service is None:
        return 'concept'

    try:
        name_emb = embedding_service.embed_for_storage(name)
        best_type = "concept"
        best_sim = -1.0

        for etype, prototypes in _ENTITY_PROTOTYPES.items():
            proto_emb = _get_prototype_embedding(embedding_service, prototypes)
            sim = sum(a * b for a, b in zip(name_emb, proto_emb))
            norm = (sum(a * a for a in name_emb) ** 0.5) * (sum(b * b for b in proto_emb) ** 0.5)
            if norm > 0:
                sim = sim / norm
            if sim > best_sim:
                best_sim = sim
                best_type = etype

        return best_type
    except Exception:
        return 'concept'


def extract_noun_phrases(text: str) -> list[str]:
    """Extract noun phrases using simple pattern matching. Filters stopword-only phrases."""
    phrases = set()

    patterns = [
        r'\b(?:the|a|an|this|that|these|those)\s+'
        r'(?:[A-Z][a-z]+\s+)*[A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+))*\b',
        r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b',
        r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b',
        r'\b[A-Z]{2,}\b',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(0).strip()
            words = candidate.split()
            if len(words) > 4:
                continue
            if len(candidate) < 3:
                continue
            if candidate.lower() in {'the', 'and', 'or'}:
                continue
            if not is_content_phrase(words):
                continue
            phrases.add(candidate)

    return list(phrases)


# SVO Extraction functions for Phase 2
PREDICATE_MAP = {
    "work": "works_at",
    "develop": "developed",
    "found": "founded",
    "create": "created",
    "use": "uses",
    "build": "built",
    "lead": "leads",
    "write": "wrote",
    "publish": "published",
    "implement": "implemented",
    "design": "designed",
    "manage": "manages",
    "join": "joined",
    "acquire": "acquired",
    "invest": "invested_in",
    "locate": "located_in",
    "hold": "held",
    "meet": "met_with",
}

_GROQ_TRIPLE_PROMPT = """You are a precise relationship extraction system. Extract every explicit subject-verb-object relationship from the text.

OUTPUT FORMAT (one per line): subject | predicate | object | confidence

Valid predicates: works_at, founded, developed, created, uses, built, leads, wrote, published, implemented, designed, manages, joined, acquired, invested_in, met_with, held, located_in, related_to

RULES:
1. Subject and object MUST be specific named entities (people, organizations, products, places, events)
2. Never use generic nouns: paper, study, company, report, system, product, research, analysis
3. Map the text verb to the closest predicate; if none matches use related_to
4. Extract every explicit relationship — do not skip any
5. Output ONLY the pipe-delimited lines — no introductions, no explanations, no markdown"""

_GROQ_TRIPLE_EXAMPLES = [
    ("Sam Altman founded OpenAI.", "Sam Altman | founded | OpenAI | 0.99"),
    ("Microsoft acquired Activision Blizzard.", "Microsoft | acquired | Activision Blizzard | 0.99"),
    ("Elon Musk leads Tesla and SpaceX.", "Elon Musk | leads | Tesla | 0.99\nElon Musk | leads | SpaceX | 0.99"),
    ("Satya Nadella is CEO of Microsoft. He met with Sundar Pichai at Davos.", "Satya Nadella | works_at | Microsoft | 0.99\nSatya Nadella | met_with | Sundar Pichai | 0.99"),
    ("Apple held WWDC 2026 at Apple Park.", "Apple | held | WWDC 2026 | 0.99\nWWDC 2026 | located_in | Apple Park | 0.8"),
]

def findSVOs(doc):
    """Extract Subject-Verb-Object triples from spaCy doc using dependency parsing."""
    svos = []
    
    # Find root verbs and their dependents
    for token in doc:
        if token.dep_ in ["ROOT", "conj"] and token.pos_ == "VERB":
            subject = None
            direct_object = None
            
            # Find subject
            for child in token.children:
                if child.dep_ in ("nsubj", "nsubjpass"):
                    subject = child
                elif child.dep_ == "dobj":
                    direct_object = child
            
            # Handle passive voice
            if not subject:
                for child in token.children:
                    if child.dep_ == "nsubjpass":
                        subject = child
            
            # If we have both subject and object, create SVO
            if subject and direct_object:
                svos.append((subject.text, token.lemma_, direct_object.text))
                
                # Handle conjunctions in subjects or objects
                for child in subject.children:
                    if child.dep_ == "conj":
                        svos.append((child.text, token.lemma_, direct_object.text))
                
                for child in direct_object.children:
                    if child.dep_ == "conj":
                        svos.append((subject.text, token.lemma_, child.text))
    
    return svos


def extract_triples_with_spacy(text: str) -> list[dict]:
    """Extract SVO triples from text using spaCy dependency parsing."""
    nlp = _get_nlp()
    
    if nlp is None:
        return []
    
    doc = nlp(text)
    svos = findSVOs(doc)
    triples = []
    
    for subj, verb, obj in svos:
        predicate = PREDICATE_MAP.get(verb, "related_to")
        
        try:
            emb_service = get_embedding_service()
            subj_emb = emb_service.embed_for_storage(subj)
            obj_emb = emb_service.embed_for_storage(obj)
            
            dot_product = sum(a * b for a, b in zip(subj_emb, obj_emb))
            norm_a = sum(a * a for a in subj_emb) ** 0.5
            norm_b = sum(b * b for b in obj_emb) ** 0.5
            confidence = 0.0
            if norm_a > 0 and norm_b > 0:
                confidence = dot_product / (norm_a * norm_b)
            
            confidence = max(0.0, min(1.0, confidence))
            
            subj_type = infer_entity_type(subj)
            obj_type = infer_entity_type(obj)
            
            if not validate_predicate(subj_type, predicate, obj_type):
                predicate = "related_to"
            
            triples.append({
                "subject": subj,
                "predicate": predicate,
                "object": obj,
                "confidence": confidence
            })
        except:
            triples.append({
                "subject": subj,
                "predicate": predicate,
                "object": obj,
                "confidence": 0.5
            })
    
    return triples


_GROQ_GENERIC_OBJECTS = {
    "paper", "study", "company", "report", "system", "product",
    "research", "analysis", "article", "document", "book", "chapter",
    "section", "page", "data", "information", "work", "project",
    "service", "platform", "tool", "application", "software", "hardware",
    "model", "algorithm", "method", "approach", "technique", "framework",
    "version", "release", "update", "patch", "build", "code", "website",
    "site", "page", "file", "document", "image", "video", "audio",
}

def extract_triples_with_groq(text: str) -> list[dict]:
    key = _get_groq_key()
    if not key:
        return []

    messages = [{"role": "system", "content": _GROQ_TRIPLE_PROMPT}]
    for user_text, assistant_text in _GROQ_TRIPLE_EXAMPLES:
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})
    messages.append({"role": "user", "content": text})

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": "llama-3.1-8b-instant",
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 512,
                "stop": ["\n\n"],
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return []

    triples = []
    seen = set()
    for line in content.split("\n"):
        line = line.strip()
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        subj, predicate, obj = parts[0], parts[1].lower(), parts[2]
        if len(subj) < 2 or len(obj) < 2:
            continue
        if obj.lower() in _GROQ_GENERIC_OBJECTS:
            continue
        conf = 0.7
        if len(parts) >= 4:
            try:
                conf = min(1.0, max(0.0, float(parts[3])))
            except ValueError:
                pass
        key = f"{subj.lower()}|{predicate}|{obj.lower()}"
        if key in seen:
            continue
        seen.add(key)
        triples.append({
            "subject": subj,
            "predicate": predicate,
            "object": obj,
            "confidence": conf,
        })
    return triples


def extract_triples(text: str) -> list[dict]:
    method = os.getenv("EXTRACTION_METHOD", "auto")
    if method == "groq":
        triples = extract_triples_with_groq(text)
        if triples:
            return triples
    if method in ("groq", "auto"):
        triples = extract_triples_with_groq(text)
        if triples:
            return triples
    return extract_triples_with_spacy(text)


# Cache for embedding calculations
_EMBEDDING_CACHE = {}
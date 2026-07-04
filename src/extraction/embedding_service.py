"""
Embedding Service for sieveon
Provides text embedding capabilities for storage and query operations
"""

import os
from typing import List, Optional

import numpy as np
from dotenv import load_dotenv


class BaseEmbeddingService:
    """Abstract base class for embedding services in sieveon"""

    # Initialize dotenv when the module is loaded
    load_dotenv()

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model = None
        self.dimension = None

    def embed(self, text: str) -> List[float]:
        """Erstellt ein Embedding für einen einzelnen Text (default: storage)"""
        return self.embed_for_storage(text)

    def embed_for_storage(self, text: str) -> List[float]:
        """Erstellt ein Embedding für die Speicherung im Event Log"""
        raise NotImplementedError

    def embed_for_query(self, text: str) -> List[float]:
        """Erstellt ein Embedding für Suchanfragen"""
        raise NotImplementedError

    def embed_batch(
        self, texts: List[str], for_storage: bool = True
    ) -> List[List[float]]:
        """Erstellt Embeddings für mehrere Texte (Batch)"""
        raise NotImplementedError


class SentenceTransformerEmbeddingService(BaseEmbeddingService):
    """Embedding Service mit sentence-transformers (open-source)"""

    def __init__(self, model_name: str = "nomic-ai/nomic-embed-text-v1.5"):
        super().__init__(model_name)

        # Dynamische Imports, nur wenn benötigt
        try:
            from sentence_transformers import SentenceTransformer
            from transformers import AutoTokenizer
        except ImportError:
            raise ImportError(
                "Bitte installiere die Abhängigkeiten: pip install sentence-transformers transformers torch"
            )

        # Initialisiere Modell und Tokenizer
        self.model = SentenceTransformer(model_name, trust_remote_code=True)
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name, trust_remote_code=True
        )
        self.dimension = self.model.get_sentence_embedding_dimension()

    def embed_for_storage(self, text: str) -> List[float]:
        """Erstellt ein Embedding für die Speicherung mit speziellem Präfix für bessere Retrieval-Qualität"""
        prefix = "search_document: "
        if self.model_name.startswith("nomic-"):
            text = f"{prefix}{text}"
        embedding = self.model.encode(
            [text], convert_to_numpy=True, normalize_embeddings=True
        )
        return embedding[0].tolist()

    def embed_for_query(self, text: str) -> List[float]:
        """Erstellt ein Embedding für Suchanfragen mit speziellem Präfix"""
        prefix = "search_query: "
        if self.model_name.startswith("nomic-"):
            text = f"{prefix}{text}"
        embedding = self.model.encode(
            [text], convert_to_numpy=True, normalize_embeddings=True
        )
        return embedding[0].tolist()

    def embed_batch(
        self, texts: List[str], for_storage: bool = True
    ) -> List[List[float]]:
        """Erstellt Embeddings für mehrere Texte (Batch)"""
        prefix = "search_document: " if for_storage else "search_query: "
        if self.model_name.startswith("nomic-"):
            texts = [f"{prefix}{t}" for t in texts]
        embeddings = self.model.encode(
            texts, convert_to_numpy=True, normalize_embeddings=True
        )
        return embeddings.tolist()


# Singleton instance (to ensure the model is only loaded once)
_embedding_service: Optional[BaseEmbeddingService] = None


def get_embedding_service() -> BaseEmbeddingService:
    global _embedding_service
    if _embedding_service is None:
        # Try to load model name from environment variables
        env_model_name = os.getenv("EMBEDDING_MODEL_NAME")
        model_name_to_use = (
            env_model_name if env_model_name else "nomic-ai/nomic-embed-text-v1.5"
        )  # Default model name
        print(
            f"[INFO] Initializing Embedding Service with model: {model_name_to_use}..."
        )
        _embedding_service = SentenceTransformerEmbeddingService(model_name_to_use)
        print("[OK] Embedding Service initialized")
    return _embedding_service


# Kleiner Test
if __name__ == "__main__":
    print("=== Embedding Service Test ===\n")
    service = get_embedding_service()

    test_texts = [
        "Alice Smith works at Acme Corp.",
        "Die Konferenz wurde auf den 15. März verschoben",
    ]

    test_text = "This is a test sentence for embedding."
    embedding = service.embed_for_storage(test_text)

    print(f"Embedding dimension: {len(embedding)}")
    print(f"First 5 values: {embedding[:5]}")
    print(f"Sample embedding for: '{test_text}'")

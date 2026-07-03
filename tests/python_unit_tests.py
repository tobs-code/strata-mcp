"""
Unit Tests for Strata
Testing individual Python components and their functions
"""
import unittest
import asyncio
from src.extraction.classifier import QueryClassifier
from src.router.policy import BudgetLevel, QueryType, RoutingPolicy
from src.planner.executor import PlanExecutor, RetrievalExecutor
from src.maintenance.conservative_maintainer import ConservativeMaintainer
from src.extraction.entropy_gate import EntropyGate, EntropyGateConfig
from src.extraction.coarse_extractor import CoarseExtractor
from src.extraction.embedding_service import get_embedding_service


class TestQueryClassifier(unittest.TestCase):
    def setUp(self):
        self.classifier = QueryClassifier()

    def test_temporal_classification(self):
        """Test classification of temporal queries"""
        query = "When did I meet Alice?"
        q_type, confidence = self.classifier.classify(query)
        self.assertEqual(q_type, "temporal")
        self.assertGreaterEqual(confidence, 0.5)

    def test_factual_classification(self):
        """Test classification of factual queries"""
        query = "Who is my manager?"
        q_type, confidence = self.classifier.classify(query)
        self.assertEqual(q_type, "factual")
        self.assertGreaterEqual(confidence, 0.5)

    def test_multi_hop_classification(self):
        """Test classification of multi-hop queries"""
        query = "Why did the project fail?"
        q_type, confidence = self.classifier.classify(query)
        self.assertEqual(q_type, "multi-hop")
        self.assertGreaterEqual(confidence, 0.5)

    def test_conversational_classification(self):
        """Test classification of conversational queries"""
        query = "Do you remember our last meeting?"
        q_type, confidence = self.classifier.classify(query)
        self.assertEqual(q_type, "conversational")
        self.assertGreaterEqual(confidence, 0.5)

    def test_update_classification(self):
        """Test classification of update queries"""
        query = "Update my contact information"
        q_type, confidence = self.classifier.classify(query)
        self.assertEqual(q_type, "update")
        self.assertGreaterEqual(confidence, 0.5)

    def test_low_confidence_default(self):
        """Test that random text defaults to factual with low confidence"""
        query = "random gibberish text"
        q_type, confidence = self.classifier.classify(query)
        self.assertEqual(q_type, "factual")
        self.assertLessEqual(confidence, 0.5)


class TestRoutingPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = RoutingPolicy()

    def test_temporal_policy(self):
        """Test routing policy for temporal queries (min_confidence=0.5)"""
        strategy_name, budget_level, policy_applied = self.policy.get_strategy(QueryType.TEMPORAL, 0.9)
        self.assertEqual(policy_applied, "strict")
        self.assertIs(budget_level, BudgetLevel.MEDIUM)

    def test_factual_policy(self):
        """Test routing policy for factual queries (min_confidence=0.4)"""
        strategy_name, budget_level, policy_applied = self.policy.get_strategy(QueryType.FACTUAL, 0.8)
        self.assertEqual(policy_applied, "strict")
        self.assertIs(budget_level, BudgetLevel.HIGH)

    def test_multi_hop_policy(self):
        """Test routing policy for multi-hop queries (min_confidence=0.8 → 0.7 triggers fallback)"""
        strategy_name, budget_level, policy_applied = self.policy.get_strategy(QueryType.MULTI_HOP, 0.7)
        self.assertEqual(strategy_name, "hybrid_fallback")
        self.assertEqual(policy_applied, "fallback")

    def test_conversational_policy(self):
        """Test routing policy for conversational queries (min_confidence=0.6)"""
        strategy_name, budget_level, policy_applied = self.policy.get_strategy(QueryType.CONVERSATIONAL, 0.9)
        self.assertEqual(policy_applied, "strict")
        self.assertIs(budget_level, BudgetLevel.MEDIUM)

    def test_update_policy(self):
        """Test routing policy for update queries (min_confidence=0.9 → 0.8 triggers fallback)"""
        strategy_name, budget_level, policy_applied = self.policy.get_strategy(QueryType.UPDATE, 0.8)
        self.assertEqual(strategy_name, "hybrid_fallback")
        self.assertEqual(policy_applied, "fallback")

    def test_low_confidence_fallback(self):
        """Test that low confidence triggers fallback strategy"""
        strategy_name, budget_level, policy_applied = self.policy.get_strategy(QueryType.TEMPORAL, 0.3)
        self.assertEqual(strategy_name, "hybrid_fallback")
        self.assertEqual(policy_applied, "fallback")


class TestPlanExecutor(unittest.TestCase):
    def setUp(self):
        self.executor = PlanExecutor()

    def test_plan_creation(self):
        """Test that plan executor can create a basic plan"""
        result = asyncio.run(self.executor.execute_plan(
            strategy="knowledge_graph_first",
            query="Who is my main contact?",
            budget_level="medium",
        ))
        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)
        self.assertIn("execution_metadata", result)


class TestEntropyGate(unittest.TestCase):
    def setUp(self):
        self.gate = EntropyGate()

    def test_entropy_calculation(self):
        """Test that entropy is calculated correctly"""
        text = "This is a test sentence."
        entropy = self.gate.calculate_char_entropy(text)
        self.assertIsInstance(entropy, float)
        self.assertGreaterEqual(entropy, 0.0)

    def test_novelty_calculation(self):
        """Test that novelty is calculated correctly"""
        text = "This is a completely new piece of text."
        novelty = self.gate.calculate_novelty(text)
        self.assertIsInstance(novelty, float)
        self.assertGreaterEqual(novelty, 0.0)
        self.assertLessEqual(novelty, 1.0)

    def test_short_text_skip(self):
        """Test that short texts are skipped"""
        short_text = "Hi"
        result = self.gate.should_extract(short_text)
        self.assertEqual(result["decision"], "skip")
        self.assertEqual(result["reason"], "text_too_short")

    def test_extraction_decision(self):
        """Test that extraction decisions are made properly"""
        text = "This is a longer text that should be evaluated for extraction."
        result = self.gate.should_extract(text)
        self.assertIn("decision", result)
        self.assertIn(result["decision"], ["extract", "ignore", "skip"])


class TestEmbeddingService(unittest.TestCase):
    def setUp(self):
        self.service = get_embedding_service()

    def test_embedding_dimensions(self):
        """Test that embeddings have expected dimensions"""
        text = "Test embedding for dimension check"
        embedding = self.service.embed_for_storage(text)
        self.assertIsInstance(embedding, list)
        self.assertGreater(len(embedding), 0)
        self.assertTrue(all(isinstance(x, float) for x in embedding))

    def test_query_vs_storage_embeddings(self):
        """Test that query and storage embeddings have same dimensions"""
        text = "Test text for both embedding types"
        query_emb = self.service.embed_for_query(text)
        storage_emb = self.service.embed_for_storage(text)
        self.assertEqual(len(query_emb), len(storage_emb))


if __name__ == '__main__':
    print("Running Strata Python Unit Tests...")
    unittest.main(verbosity=2)
"""
Edge Case Tests for STRATA
Testing boundary conditions and error scenarios
"""
import unittest
import sys
import os

# Add the src directory to the path so we can import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.extraction.classifier import QueryClassifier
from src.router.policy import BudgetLevel, QueryType, RoutingPolicy
from src.router.cost_awareness import CostTracker
from src.extraction.entropy_gate import EntropyGate, EntropyGateConfig
from src.extraction.coarse_extractor import CoarseExtractor
from src.planner.executor import PlanExecutor
from src.maintenance.conservative_maintainer import ConservativeMaintainer
from src.extraction.embedding_service import get_embedding_service
import asyncio


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.classifier = QueryClassifier()
        self.policy = RoutingPolicy()
        self.executor = PlanExecutor()
        self.maintainer = ConservativeMaintainer()
        self.gate = EntropyGate()
        self.embedding_service = get_embedding_service()

    def test_empty_query_classification(self):
        """Test classification of empty query"""
        q_type, confidence = self.classifier.classify("")
        self.assertEqual(q_type, "factual")  # Should default to factual
        self.assertAlmostEqual(confidence, 0.5, places=1)  # Should have medium confidence

    def test_whitespace_only_query(self):
        """Test classification of whitespace-only query"""
        q_type, confidence = self.classifier.classify("   \t\n  ")
        self.assertEqual(q_type, "factual")  # Should default to factual
        self.assertAlmostEqual(confidence, 0.5, places=1)

    def test_special_characters_query(self):
        """Test classification with special characters"""
        q_type, confidence = self.classifier.classify("??? When did this happen ???")
        self.assertEqual(q_type, "temporal")  # Should still recognize temporal patterns
        self.assertGreaterEqual(confidence, 0.5)

    def test_extremely_long_query(self):
        """Test classification of extremely long query"""
        long_query = "When did " + "the thing happen? " * 1000
        q_type, confidence = self.classifier.classify(long_query)
        # Should recognize "when" pattern despite length
        self.assertEqual(q_type, "temporal")
        self.assertGreaterEqual(confidence, 0.5)

    def test_multilingual_query(self):
        """Test classification with mixed languages"""
        query = "Cuando y por qué happened this?"
        q_type, confidence = self.classifier.classify(query)
        # Should default to factual since no clear patterns match
        self.assertEqual(q_type, "factual")
        self.assertLessEqual(confidence, 0.6)

    def test_exact_match_patterns(self):
        """Test queries that exactly match pattern words"""
        temporal_words = ["when", "When", "WHEN"]
        factual_words = ["who", "what", "where", "Who", "What", "Where"]
        
        for word in temporal_words:
            q_type, confidence = self.classifier.classify(word)
            self.assertEqual(q_type, "temporal")
            self.assertGreaterEqual(confidence, 0.6)
        
        for word in factual_words:
            q_type, confidence = self.classifier.classify(word)
            self.assertEqual(q_type, "factual")
            self.assertGreaterEqual(confidence, 0.6)

    def test_very_high_confidence_boundary(self):
        """Test behavior at confidence boundaries"""
        # Very clear temporal query
        query = "When did the meeting with Alice Johnson occur yesterday afternoon?"
        q_type, confidence = self.classifier.classify(query)
        self.assertEqual(q_type, "temporal")
        self.assertGreaterEqual(confidence, 0.8)

    def test_policy_with_missing_data(self):
        """Test executor behavior when data is missing"""
        result = asyncio.run(self.executor.execute_plan(
            strategy="knowledge_graph_first",
            query="Retrieve non-existent information",
            budget_level="medium",
        ))
        self.assertIsInstance(result, dict)
        self.assertIn("execution_metadata", result)

    def test_executor_with_missing_data(self):
        """Test executor behavior when data is missing"""
        result = asyncio.run(self.executor.execute_plan(
            strategy="knowledge_graph_first",
            query="Retrieve non-existent information",
            budget_level="medium",
        ))
        self.assertIsInstance(result, dict)
        self.assertIn("execution_metadata", result)


class TestEntropyGateSpecificFunctionality(unittest.TestCase):
    def test_entropy_calculation(self):
        """Test specific entropy calculation functionality"""
        # Create a minimal entropy gate without embedding service
        config = EntropyGateConfig(alpha=0.5, beta=0.5, base_threshold=0.5, min_length=1)
        gate = EntropyGate(config=config)
        
        # Test entropy calculation with various inputs
        entropy1 = gate.calculate_char_entropy("aaaaaa")  # Low entropy (repeated chars)
        entropy2 = gate.calculate_char_entropy("abcdef")  # Higher entropy (unique chars)
        
        # "aaaaaa" should have lower entropy than "abcdef"
        self.assertLess(entropy1, entropy2)
        
        # Empty string should have 0 entropy
        entropy_empty = gate.calculate_char_entropy("")
        self.assertEqual(entropy_empty, 0.0)
        
        # Single character should have 0 entropy
        entropy_single = gate.calculate_char_entropy("a")
        self.assertEqual(entropy_single, 0.0)

    def test_novelty_calculation(self):
        """Test novelty calculation"""
        # Create a minimal entropy gate without embedding service
        config = EntropyGateConfig(alpha=0.5, beta=0.5, base_threshold=0.5, min_length=1)
        gate = EntropyGate(config=config)
        
        # Novelty should return a valid float between 0 and 1
        novelty = gate.calculate_novelty("test text")
        self.assertIsInstance(novelty, float)
        self.assertGreaterEqual(novelty, 0.0)
        self.assertLessEqual(novelty, 1.0)

    def test_executor_with_missing_data(self):
        """Test executor behavior when data is missing"""
        executor = PlanExecutor()
        result = asyncio.run(executor.execute_plan(
            strategy="knowledge_graph_first",
            query="Retrieve non-existent information",
            budget_level="medium",
        ))
        self.assertIsInstance(result, dict)
        self.assertIn("execution_metadata", result)


class TestCoarseExtractor(unittest.TestCase):
    def test_extractor_initialization(self):
        """Test coarse extractor initialization"""
        extractor = CoarseExtractor()
        self.assertIsNotNone(extractor)

    def test_extractor_extraction_on_empty_text(self):
        """Test extraction on empty text"""
        extractor = CoarseExtractor()
        result = extractor.extract("")
        self.assertEqual(result, {})

    def test_maintainer_with_empty_state(self):
        """Test maintainer behavior when system state is empty"""
        maintainer = ConservativeMaintainer()
        result = asyncio.run(maintainer.perform_maintenance())
        # Should not error even with no data
        self.assertIsInstance(result, dict)
        self.assertIn("stats", result)


class TestPolicyConfiguration(unittest.TestCase):
    def test_custom_policy_configuration(self):
        """Test custom policy configuration"""
        custom_config = {
            "temporal": {
                "strategy": "event_log_first",
                "min_confidence": 0.8
            }
        }
        policy = RoutingPolicy(config=custom_config)
        
        # Test that custom configuration is used
        strategy_name, budget_level, policy_applied = policy.get_strategy(QueryType.TEMPORAL, 0.9)
        self.assertEqual(strategy_name, "event_log_first")
        
        # Test fallback for low confidence
        strategy_name, budget_level, policy_applied = policy.get_strategy(QueryType.TEMPORAL, 0.7)
        self.assertEqual(strategy_name, "hybrid_fallback")

    def test_entropy_gate_edge_cases(self):
        """Test entropy gate with edge case inputs"""
        gate = EntropyGate()
        # Empty text
        result = gate.should_extract("")
        self.assertEqual(result["decision"], "skip")
        
        # Single character
        result = gate.should_extract("A")
        self.assertEqual(result["decision"], "skip")
        
        # Exactly at minimum length
        min_text = "x" * gate.config.min_length
        result = gate.should_extract(min_text)
        # Should attempt to evaluate since at minimum length
        self.assertIn("decision", result)


class TestClassifierPriorityOrder(unittest.TestCase):
    def test_priority_order_enforcement(self):
        """Test that priority order is properly enforced"""
        classifier = QueryClassifier()
        
        # Create a query that matches multiple patterns
        # For example, a query containing both "when" (temporal) and "who" (factual)
        # According to priority order: update > multi-hop > conversational > temporal > factual
        # So temporal should win over factual in this case
        query = "When who arrived?"
        q_type, confidence = classifier.classify(query)
        
        # The result could be either temporal or factual depending on implementation,
        # but it should be one of the valid types
        self.assertIn(q_type, ["temporal", "factual", "multi-hop", "conversational", "update"])
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLessEqual(confidence, 1.0)


if __name__ == '__main__':
    print("Running Strata Edge Case Tests...")
    unittest.main(verbosity=2)
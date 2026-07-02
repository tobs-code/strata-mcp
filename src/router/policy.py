"""
Routing Policy with cost awareness and adaptive enforcement.
Implements adaptive routing based on query type and learned strategy effectiveness.
"""

from typing import Dict, Optional, Any, Tuple
from enum import Enum
import threading
import time
from datetime import datetime, timedelta
import logging
from ..cost_awareness import cost_tracker


class QueryType(Enum):
    TEMPORAL = "temporal"
    FACTUAL = "factual"
    MULTI_HOP = "multi_hop"
    CONVERSATIONAL = "conversational"
    UPDATE = "update"


class BudgetLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OverBudget(Exception):
    """Raised when a request exceeds its allocated budget."""
    pass


class BudgetTracker:
    """
    Tracks resource consumption for individual requests.
    Uses a global health factor to adaptively scale limits based on system health.
    """
    # Class-level health factor to ensure all trackers react to system health simultaneously
    _health_factor = 1.0  # 1.0 = healthy, 0.1 = very unhealthy
    _health_lock = threading.Lock()
    
    def __init__(self):
        self.db_calls = 0
        self.estimated_tokens = 0
        self.start_time = time.time()
        
        # Default limits per budget level
        self.limits = {
            BudgetLevel.LOW: {
                'db_calls': 10,
                'tokens': 1000
            },
            BudgetLevel.MEDIUM: {
                'db_calls': 25,
                'tokens': 3000
            },
            BudgetLevel.HIGH: {
                'db_calls': 50,
                'tokens': 8000
            }
        }

    @classmethod
    def update_system_health(cls, factor: float):
        """Update the global health factor."""
        with cls._health_lock:
            cls._health_factor = max(0.1, min(1.0, factor))

    @classmethod
    def get_system_health(cls) -> float:
        """Get the current system health factor."""
        with cls._health_lock:
            return cls._health_factor

    def track_db_call(self):
        """Increment database call counter."""
        self.db_calls += 1

    def track_tokens(self, count: int):
        """Increment token counter."""
        self.estimated_tokens += count

    def is_over_budget(self, budget_level: BudgetLevel) -> bool:
        """Check if current usage exceeds budget."""
        limits = self.limits[budget_level]
        health_factor = self.get_system_health()
        
        # Scale limits based on system health
        max_db = int(limits['db_calls'] * health_factor)
        max_tokens = int(limits['tokens'] * health_factor)
        
        return (self.db_calls > max_db or self.estimated_tokens > max_tokens)

    def get_remaining_budget(self, budget_level: BudgetLevel) -> Dict[str, int]:
        """Get remaining budget."""
        limits = self.limits[budget_level]
        health_factor = self.get_system_health()
        
        max_db = int(limits['db_calls'] * health_factor)
        max_tokens = int(limits['tokens'] * health_factor)
        
        return {
            'remaining_db_calls': max(0, max_db - self.db_calls),
            'remaining_tokens': max(0, max_tokens - self.estimated_tokens)
        }


class RoutingPolicy:
    """
    Determines routing strategy based on query type and learned effectiveness.
    Now incorporates adaptive cost-awareness using CostTracker metrics.
    """
    
    def __init__(self):
        # Thread safety for concurrent access
        self._lock = threading.RLock()
        
        # Store usage patterns per query type (timestamp-based for cleanup)
        self._usage: Dict[str, Dict[str, Any]] = {}
        
        # Default configuration per query type
        self.config = {
            QueryType.TEMPORAL: {
                'strategy': 'event_log_first',
                'budget': BudgetLevel.MEDIUM,
                'min_confidence': 0.5,
                'max_latency_threshold': 1.0  # seconds
            },
            QueryType.FACTUAL: {
                'strategy': 'knowledge_graph_first',
                'budget': BudgetLevel.MEDIUM,
                'min_confidence': 0.7,
                'max_latency_threshold': 0.8
            },
            QueryType.MULTI_HOP: {
                'strategy': 'hybrid_with_graph_expansion',
                'budget': BudgetLevel.HIGH,
                'min_confidence': 0.8,
                'max_latency_threshold': 2.0
            },
            QueryType.CONVERSATIONAL: {
                'strategy': 'hybrid_bm25_vector_temporal',
                'budget': BudgetLevel.MEDIUM,
                'min_confidence': 0.6,
                'max_latency_threshold': 1.2
            },
            QueryType.UPDATE: {
                'strategy': 'knowledge_graph_with_invalidation',
                'budget': BudgetLevel.HIGH,
                'min_confidence': 0.9,
                'max_latency_threshold': 1.5
            }
        }
        
        # Cleanup interval for usage tracking (avoid memory leaks)
        self._cleanup_interval = timedelta(minutes=30)
        self._last_cleanup = datetime.now()

    def _cleanup_old_usage(self):
        """Remove old usage entries to prevent memory leaks."""
        now = datetime.now()
        if now - self._last_cleanup > self._cleanup_interval:
            with self._lock:
                # Remove entries older than 1 hour
                cutoff = now - timedelta(hours=1)
                keys_to_remove = [
                    k for k, v in self._usage.items()
                    if v.get('timestamp', datetime.min) < cutoff
                ]
                
                for key in keys_to_remove:
                    del self._usage[key]
                
                self._last_cleanup = now

    def _get_adapted_strategy(self, query_type: QueryType) -> str:
        """
        Get the most effective strategy for a query type based on learned metrics.
        Falls back to config default if no learning data available.
        """
        with self._lock:
            # Get base strategy from config
            base_config = self.config.get(query_type)
            if not base_config:
                # Fallback to factual if unknown query type
                base_config = self.config[QueryType.FACTUAL]
                logging.warning(f"Unknown query type {query_type}, falling back to factual config")
            
            base_strategy = base_config['strategy']
            
            # Get ranked strategies from cost tracker
            ranked_strategies = cost_tracker.get_all_strategies_ranked()
            
            if ranked_strategies:
                # Find the best performing strategy that's appropriate for this query type
                for strategy, score in ranked_strategies:
                    # Only consider strategies that are valid for this query type
                    # We prefer strategies with good effectiveness scores
                    if self._is_strategy_appropriate_for_query_type(strategy, query_type):
                        return strategy
            
            # Fall back to configured default
            return base_strategy

    def _is_strategy_appropriate_for_query_type(self, strategy: str, query_type: QueryType) -> bool:
        """Check if a strategy is appropriate for a specific query type."""
        # Define strategy-query type compatibility
        compatible_strategies = {
            QueryType.TEMPORAL: ['event_log_first', 'hybrid_bm25_vector_temporal'],
            QueryType.FACTUAL: ['knowledge_graph_first', 'hybrid_with_graph_expansion'],
            QueryType.MULTI_HOP: ['hybrid_with_graph_expansion', 'knowledge_graph_with_invalidation'],
            QueryType.CONVERSATIONAL: ['hybrid_bm25_vector_temporal', 'composite_kg_vector'],
            QueryType.UPDATE: ['knowledge_graph_with_invalidation', 'knowledge_graph_first']
        }
        
        compatible = compatible_strategies.get(query_type, [])
        return strategy in compatible

    def get_strategy(self, query_type: QueryType, confidence: float) -> Tuple[str, BudgetLevel, str]:
        """
        Determine the optimal strategy based on query type, confidence, and learned effectiveness.
        
        Returns:
            Tuple of (strategy, budget_level, policy_applied)
        """
        with self._lock:
            # Perform periodic cleanup
            self._cleanup_old_usage()
            
            # Get base configuration
            base_config = self.config.get(query_type)
            if not base_config:
                # Fallback to factual if unknown query type
                base_config = self.config[QueryType.FACTUAL]
                logging.warning(f"Unknown query type {query_type}, falling back to factual config")
            
            # Check if confidence is high enough for primary strategy
            min_confidence = base_config['min_confidence']
            
            # Get the adapted strategy based on learned effectiveness
            if confidence >= min_confidence:
                strategy = self._get_adapted_strategy(query_type)
                policy_applied = "strict"
            else:
                # Use fallback strategy when confidence is low
                strategy = "hybrid_fallback"
                policy_applied = "fallback"
            
            budget = base_config['budget']
            
            # Record this usage pattern
            usage_key = f"{query_type.value}_{datetime.now().isoformat()}"
            self._usage[usage_key] = {
                'query_type': query_type.value,
                'strategy': strategy,
                'confidence': confidence,
                'budget': budget.value,
                'timestamp': datetime.now(),
                'policy_applied': policy_applied
            }
            
            return strategy, budget, policy_applied

    def get_budget_for_query(self, query_type: QueryType) -> BudgetLevel:
        """Get the appropriate budget level for a query type."""
        with self._lock:
            base_config = self.config.get(query_type)
            if not base_config:
                base_config = self.config[QueryType.FACTUAL]
            return base_config['budget']

    def update_query_config(self, query_type: QueryType, **kwargs):
        """Update configuration for a specific query type."""
        with self._lock:
            if query_type in self.config:
                self.config[query_type].update(kwargs)
            else:
                raise ValueError(f"Unknown query type: {query_type}")

    def get_all_costs(self) -> Dict:
        """Get all cost information from both policy and cost tracker."""
        with self._lock:
            return {
                'policy_config': {
                    qt.value: config for qt, config in self.config.items()
                },
                'cost_tracker_metrics': cost_tracker.get_all_costs(),
                'system_health': BudgetTracker.get_system_health(),
                'usage_patterns_count': len(self._usage)
            }
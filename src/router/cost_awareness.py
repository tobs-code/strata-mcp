from collections import defaultdict
import time
from typing import Dict, List, Optional, Tuple
import threading
from datetime import datetime, timedelta


class CostTracker:
    """
    Tracks performance metrics per strategy including latency, success rate, and cost.
    Implements adaptive cost awareness by adjusting strategy effectiveness based on relevance feedback.
    """

    def __init__(self):
        # Thread-safe storage for metrics
        self._metrics_lock = threading.RLock()
        
        # Track strategy performance: {strategy_name: {metric: value}}
        self._metrics = defaultdict(lambda: {
            'latency_sum': 0.0,
            'latency_count': 0,
            'success_count': 0,
            'total_count': 0,
            'cost_sum': 0.0,
            'cost_count': 0,
            'last_used': None
        })
        
        # Base costs per strategy (reflects computational complexity)
        self.base_costs = {
            'event_log_first': 0.5,
            'knowledge_graph_first': 1.0,
            'hybrid_with_graph_expansion': 1.2,
            'composite_kg_vector': 1.0,
            'knowledge_graph_with_invalidation': 2.0,  # Most expensive due to writes
            'hybrid_bm25_vector_temporal': 1.1,
            'hybrid_fallback': 0.8
        }

    def record_request(self, strategy: str, latency: float, success: bool, num_queries: int = 1, relevance: float = 1.0):
        """
        Records a request with its performance metrics.
        
        Args:
            strategy: The strategy used
            latency: Time taken in seconds
            success: Whether the request was successful
            num_queries: Number of database queries made
            relevance: Relevance score of results (0.0 to 1.0)
        """
        with self._metrics_lock:
            metrics = self._metrics[strategy]
            
            # Update basic metrics
            metrics['latency_sum'] += latency
            metrics['latency_count'] += 1
            
            if success:
                metrics['success_count'] += 1
            metrics['total_count'] += 1
            
            # Calculate cost using formula: base_cost * num_queries * (1.0 + (1.0 - relevance))
            base_cost = self.base_costs.get(strategy, 1.0)
            calculated_cost = base_cost * num_queries * (1.0 + (1.0 - relevance))
            
            metrics['cost_sum'] += calculated_cost
            metrics['cost_count'] += 1
            metrics['last_used'] = datetime.now()

    def get_average_latency(self, strategy: str) -> Optional[float]:
        """Returns average latency for a strategy."""
        with self._metrics_lock:
            metrics = self._metrics[strategy]
            if metrics['latency_count'] > 0:
                return metrics['latency_sum'] / metrics['latency_count']
            return None

    def get_success_rate(self, strategy: str) -> Optional[float]:
        """Returns success rate for a strategy."""
        with self._metrics_lock:
            metrics = self._metrics[strategy]
            if metrics['total_count'] > 0:
                return metrics['success_count'] / metrics['total_count']
            return None

    def get_average_cost(self, strategy: str) -> Optional[float]:
        """Returns average cost for a strategy."""
        with self._metrics_lock:
            metrics = self._metrics[strategy]
            if metrics['cost_count'] > 0:
                return metrics['cost_sum'] / metrics['cost_count']
            return None

    def get_effectiveness_score(self, strategy: str) -> float:
        """
        Returns a combined effectiveness score (0.0 to 1.0) based on:
        - Success rate (higher is better)
        - Average cost (lower is better)
        - Recency of usage (more recent is better)
        
        Lower score means more effective strategy.
        """
        with self._metrics_lock:
            metrics = self._metrics[strategy]
            
            # Get success rate (higher is better)
            success_rate = self.get_success_rate(strategy) or 0.0
            
            # Get average cost (lower is better, normalize to 0-1 scale)
            avg_cost = self.get_average_cost(strategy)
            if avg_cost is not None:
                # Normalize cost to 0-1 scale (assuming max reasonable cost of 5.0)
                cost_efficiency = max(0.0, min(1.0, (5.0 - avg_cost) / 5.0))
            else:
                cost_efficiency = 0.5  # Default if no data
            
            # Consider recency (strategies used more recently might be preferred)
            recency_factor = 1.0
            if metrics['last_used']:
                time_since = datetime.now() - metrics['last_used']
                # Reduce score for strategies not used in last hour
                if time_since > timedelta(hours=1):
                    recency_factor = 0.8
            
            # Combined score (lower is better)
            effectiveness = (1.0 - success_rate) * 0.4 + cost_efficiency * 0.6
            return effectiveness * recency_factor

    def get_all_strategies_ranked(self) -> List[Tuple[str, float]]:
        """
        Returns all strategies ranked by effectiveness (best first).
        """
        with self._metrics_lock:
            strategies = []
            for strategy in self._metrics.keys():
                score = self.get_effectiveness_score(strategy)
                strategies.append((strategy, score))
            
            # Sort by effectiveness score (lowest first - most effective first)
            return sorted(strategies, key=lambda x: x[1])

    def get_all_costs(self) -> Dict[str, Dict]:
        """Returns all cost metrics."""
        with self._metrics_lock:
            result = {}
            for strategy, metrics in self._metrics.items():
                result[strategy] = {
                    'average_latency': self.get_average_latency(strategy),
                    'success_rate': self.get_success_rate(strategy),
                    'average_cost': self.get_average_cost(strategy),
                    'total_requests': metrics['total_count'],
                    'effectiveness_score': self.get_effectiveness_score(strategy)
                }
            return result

    def reset_metrics(self):
        """Resets all metrics."""
        with self._metrics_lock:
            self._metrics.clear()


# Global instance for cross-module access
cost_tracker = CostTracker()
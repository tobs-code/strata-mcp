"""
Python Router module for STRATA Memory Control Plane
Implements the same routing logic as the Rust router for consistency
"""

from enum import Enum
from typing import Dict, Tuple, Optional
import time
import logging

# Import the enhanced policy and cost tracking
from .policy import RoutingPolicy, QueryType, BudgetLevel, OverBudget
from ..cost_awareness import cost_tracker


class Router:
    """
    Python implementation of the STRATA router
    Provides identical routing logic to the Rust implementation for consistency
    """
    
    def __init__(self):
        self.routing_policy = RoutingPolicy()
        self.budget_trackers: Dict[str, 'BudgetTracker'] = {}

    def route(self, query: str, query_type: QueryType, confidence: float) -> Tuple[str, BudgetLevel, str]:
        """
        Route a query to the appropriate strategy based on type and learned effectiveness.
        
        Args:
            query: The query string
            query_type: The classified query type
            confidence: Classification confidence (0.0-1.0)
            
        Returns:
            Tuple of (strategy, budget_level, policy_applied)
        """
        # Get strategy from enhanced policy that considers cost metrics
        strategy, budget, policy_applied = self.routing_policy.get_strategy(query_type, confidence)
        
        # Record this routing decision for cost tracking
        cost_tracker.record_request(
            strategy=strategy,
            latency=0.0,  # Will be measured during execution
            success=True,  # Assumed initially
            num_queries=1,  # Will be updated during execution
            relevance=confidence  # Use confidence as initial relevance estimate
        )
        
        return strategy, budget, policy_applied

    def execute_with_budget(self, query_type: QueryType, operation_fn, **budget_kwargs):
        """
        Execute an operation with budget enforcement.
        
        Args:
            query_type: The query type to determine budget
            operation_fn: The function to execute
            **budget_kwargs: Additional budget parameters
        """
        budget_tracker = BudgetLevel.MEDIUM  # Default
        
        try:
            # Get appropriate budget for this query type
            budget_tracker = self.routing_policy.get_budget_for_query(query_type)
            
            # Execute the operation
            result = operation_fn(budget=budget_tracker, **budget_kwargs)
            
            return result
        except OverBudget as e:
            logging.warning(f"Operation exceeded budget: {e}")
            # Return fallback result or re-raise depending on requirements
            raise
        except Exception as e:
            # Record failure in cost tracker
            strategy, _, _ = self.routing_policy.get_strategy(query_type, 0.5)  # Default confidence
            cost_tracker.record_request(
                strategy=strategy,
                latency=0.0,  # Not measured due to error
                success=False,
                num_queries=0,
                relevance=0.0
            )
            raise

    def get_routing_explanation(self, query: str, query_type: QueryType, confidence: float) -> Dict:
        """
        Get explanation for routing decision.
        """
        strategy, budget, policy_applied = self.route(query, query_type, confidence)
        
        # Get cost metrics for the chosen strategy
        cost_metrics = cost_tracker.get_all_costs().get(strategy, {})
        
        return {
            "query": query,
            "classified_as": query_type.value,
            "confidence": confidence,
            "strategy_selected": strategy,
            "budget_level": budget.value,
            "policy_applied": policy_applied,
            "reason": f"Query classified as {query_type.value} with confidence {confidence}. "
                     f"Selected strategy '{strategy}' based on effectiveness metrics. "
                     f"Budget level set to {budget.value}.",
            "cost_metrics": cost_metrics,
            "system_health": self.routing_policy.get_all_costs()['system_health']
        }


# Convenience functions for compatibility with existing code
def classify_query(text: str) -> Tuple[QueryType, float]:
    """
    Simple query classification for demonstration.
    In a real implementation, this would use ML models.
    """
    text_lower = text.lower()
    
    if any(word in text_lower for word in ["when", "time", "date", "before", "after", "during"]):
        return QueryType.TEMPORAL, 0.8
    elif any(word in text_lower for word in ["what", "who", "where", "how much", "which"]):
        return QueryType.FACTUAL, 0.7
    elif any(word in text_lower for word in ["relationship", "connected", "related to", "connection"]):
        return QueryType.MULTI_HOP, 0.8
    elif any(word in text_lower for word in ["conversation", "chat", "talk", "discuss"]):
        return QueryType.CONVERSATIONAL, 0.6
    elif any(word in text_lower for word in ["update", "change", "modify", "edit", "correct"]):
        return QueryType.UPDATE, 0.9
    else:
        return QueryType.FACTUAL, 0.5  # Default fallback


def route_query(text: str) -> Tuple[str, str]:
    """
    Route a query string to appropriate strategy.
    Returns (strategy, budget_level) as strings.
    """
    router = Router()
    query_type, confidence = classify_query(text)
    strategy, budget, policy = router.route(text, query_type, confidence)
    
    return strategy, budget.value


# Export for use in other modules
__all__ = ['Router', 'QueryType', 'BudgetLevel', 'RoutingPolicy', 'route_query', 'classify_query']
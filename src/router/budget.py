"""
Unified BudgetTracker for sieveon
Tracks resource consumption per request with adaptive system health.
Single source of truth – imported by policy, executor, and MCP core.
"""

import threading
import time
from typing import Dict, Optional
from enum import Enum


class BudgetLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class BudgetTracker:
    _health_factor = 1.0
    _health_lock = threading.Lock()

    def __init__(self, budget_level: BudgetLevel = BudgetLevel.MEDIUM):
        self.db_calls = 0
        self.estimated_tokens = 0
        self.budget_level = budget_level
        self.start_time = time.time()
        self.limits = {
            BudgetLevel.LOW: {"db_calls": 10, "tokens": 1000},
            BudgetLevel.MEDIUM: {"db_calls": 25, "tokens": 3000},
            BudgetLevel.HIGH: {"db_calls": 50, "tokens": 8000},
        }

    def increment_db_calls(self, count: int = 1):
        self.db_calls += count

    def increment_tokens(self, count: int):
        self.estimated_tokens += count

    def is_over_budget(self, budget_level: Optional[BudgetLevel] = None) -> bool:
        limits = self.limits[budget_level or self.budget_level]
        health = self.get_system_health()
        return (
            self.db_calls > int(limits["db_calls"] * health)
            or self.estimated_tokens > int(limits["tokens"] * health)
        )

    def get_remaining_budget(self, budget_level: Optional[BudgetLevel] = None) -> Dict[str, int]:
        limits = self.limits[budget_level or self.budget_level]
        health = self.get_system_health()
        return {
            "remaining_db_calls": max(0, int(limits["db_calls"] * health) - self.db_calls),
            "remaining_tokens": max(0, int(limits["tokens"] * health) - self.estimated_tokens),
        }

    @classmethod
    def update_system_health(cls, factor: float):
        with cls._health_lock:
            cls._health_factor = max(0.1, min(1.0, factor))

    @classmethod
    def get_system_health(cls) -> float:
        with cls._health_lock:
            return cls._health_factor

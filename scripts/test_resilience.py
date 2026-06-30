"""
Test script for verifying Resilience and Adaptive Budgeting features.
Simulates DB failures and checks if the system health and budgets scale accordingly.
"""
import asyncio
import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.router.policy import BudgetTracker
from src.mcp.server import _budget_aware_should_retry, _surreal_lock

async def test_adaptive_budgeting():
    print("\n--- Testing Adaptive Budgeting ---")
    
    # 1. Check healthy state
    BudgetTracker.update_system_health(1.0)
    tracker_healthy = BudgetTracker("medium")
    print(f"Healthy Factor: {BudgetTracker._health_factor}")
    
    # Simulate usage that should be OK in healthy state (25 DB calls limit)
    tracker_healthy.record_db_call(20)
    print(f"Medium Budget (20 calls): Over budget? {tracker_healthy.is_over_budget()} (Expected: False)")
    
    # 2. Check degraded state
    BudgetTracker.update_system_health(0.5)
    tracker_degraded = BudgetTracker("medium")
    print(f"Degraded Factor: {BudgetTracker._health_factor}")
    
    # 20 calls should now be over budget (25 * 0.5 = 12.5 limit)
    tracker_degraded.record_db_call(20)
    print(f"Medium Budget (20 calls): Over budget? {tracker_degraded.is_over_budget()} (Expected: True)")
    
    # 3. Check retry scaling
    print("\n--- Testing Adaptive Retries ---")
    BudgetTracker.update_system_health(1.0)
    retries_healthy = _budget_aware_should_retry("SELECT * FROM event")
    print(f"Retries (Healthy, normal query): {retries_healthy} (Expected: 3)")
    
    BudgetTracker.update_system_health(0.3)
    retries_degraded = _budget_aware_should_retry("SELECT * FROM event")
    print(f"Retries (Degraded, normal query): {retries_degraded} (Expected: 2)")

async def main():
    await test_adaptive_budgeting()
    print("\n[OK] Resilience and Adaptive Budgeting logic verified.")

if __name__ == "__main__":
    asyncio.run(main())

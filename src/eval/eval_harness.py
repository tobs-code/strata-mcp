"""
Multi-dimensional Evaluation Harness
Measures the 5 key metrics from the paper:
1. Retrieval Fidelity
2. Update Robustness
3. Long-Horizon Stability
4. Latency
5. Operation Cost
"""

import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.extraction.classifier import QueryClassifier
from src.extraction.entropy_gate import escape_surrealql
from src.router.cost_awareness import CostTracker
from src.router.policy import RoutingPolicy


class SieveonEvalHarness:
    def __init__(self):
        # Database configuration from environment variables
        self.surreal_url = os.getenv("SURREALDB_URL", "http://127.0.0.1:8000/sql")
        self.auth = (
            os.getenv("SURREALDB_USER", "root"),
            os.getenv("SURREALDB_PASS", "root"),
        )
        self.ns = os.getenv("SURREALDB_NS", "sieveon")
        self.db = os.getenv("SURREALDB_DB", "sieveon")

        # Service endpoints
        self.router_endpoint = "http://127.0.0.1:8080"
        self.planner_endpoint = "http://127.0.0.1:8081"
        self.control_plane_endpoint = "http://127.0.0.1:8082"

        # Initialize components
        self.classifier = QueryClassifier()
        self.policy = RoutingPolicy()
        self.tracker = CostTracker()

        # Metrics
        self.metrics = {
            "retrieval_fidelity": 0.0,
            "update_robustness": 0.0,
            "long_horizon_stability": 0.0,
            "latency": 0.0,
            "operation_cost": 0.0,
        }

    def _query_surreal(self, sql: str) -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        full_sql = f"USE NS {self.ns} DB {self.db};\n{sql}"
        response = requests.post(
            self.surreal_url, data=full_sql, headers=headers, auth=self.auth, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("status") == "ERR":
                    raise RuntimeError(
                        f"SurrealDB Error: {item.get('information') or item.get('result')} | SQL: {sql[:120]}"
                    )
        return data

    def execute_strategy(self, strategy_name, query):
        start = time.time()

        if strategy_name == "event_log_first":
            sql = f"SELECT * FROM event WHERE content @@ '{escape_surrealql(query)}' ORDER BY timestamp DESC LIMIT 5;"
            result = self._query_surreal(sql)
            num_queries = 1
        elif strategy_name == "knowledge_graph_first":
            sql = f"SELECT * FROM entity WHERE name @@ '{escape_surrealql(query)}';"
            result = self._query_surreal(sql)
            num_queries = 1
        else:
            # Hybrid fallback
            sql1 = f"SELECT * FROM event WHERE content @@ '{escape_surrealql(query)}' LIMIT 3;"
            sql2 = f"SELECT * FROM entity WHERE name @@ '{escape_surrealql(query)}' LIMIT 3;"
            result = [self._query_surreal(sql1), self._query_surreal(sql2)]
            num_queries = 2

        latency = time.time() - start
        # Success check
        success = False
        if isinstance(result, list):
            for r in result:
                if (
                    isinstance(r, list)
                    and len(r) > 1
                    and isinstance(r[1], dict)
                    and r[1].get("result")
                ):
                    success = True
                    break
        elif (
            isinstance(result, list)
            and len(result) > 1
            and isinstance(result[1], dict)
            and result[1].get("result")
        ):
            success = True

        return result, latency, num_queries, success

    def evaluate(self, test_cases):
        print("Running sieveon Evaluation Harness...")

        results = {
            "timestamp": datetime.now().isoformat(),
            "metrics": {},
            "overall_score": 0.0,
        }

        # Run each metric
        results["metrics"]["retrieval_fidelity"] = self.measure_retrieval_fidelity()
        results["metrics"]["update_robustness"] = self.measure_update_robustness()
        results["metrics"]["long_horizon_stability"] = (
            self.measure_long_horizon_stability()
        )
        results["metrics"]["latency"] = self.measure_latency()
        results["metrics"]["operation_cost"] = self.measure_operation_cost()

        # Calculate overall score
        scores = [
            m.get("score", 0) for m in results["metrics"].values() if "score" in m
        ]
        results["overall_score"] = np.mean(scores) if scores else 0.0

        return results

    def print_summary(self, results):
        print("\n" + "=" * 50)
        print("sieveon EVALUATION RESULTS")
        print("=" * 50)

        for metric, data in results["metrics"].items():
            print(f"\n{metric.upper()}:")
            print(f"  Score: {data.get('score', 'N/A')}")
            for key, value in data.items():
                if key != "score" and not key.endswith("_score"):
                    print(f"  {key}: {value}")

        print(f"\nOVERALL SCORE: {results['overall_score']:.3f}")
        print("=" * 50)


if __name__ == "__main__":
    harness = SieveonEvalHarness()
    results = harness.evaluate()

    # The summary is already printed in run_all_evals
    # Save results to file
    with open("sieveon_eval_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Results saved to sieveon_eval_results.json")

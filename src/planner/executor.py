"""
Plan Executor for Strata
Executes different retrieval strategies based on the plan
"""
import asyncio
import time
import requests
import json
import os
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone
import numpy as np
from urllib.parse import quote

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is not installed, skip loading .env file
    pass

# Import embedding service
try:
    from src.extraction.embedding_service import get_embedding_service
except ImportError:
    # Handle relative import when module is not installed
    from extraction.embedding_service import get_embedding_service

# Import routing policy for budget tracking
try:
    from src.router.policy import RoutingPolicy, OverBudget
except ImportError:
    from router.policy import RoutingPolicy, OverBudget  # type: ignore

# Optional tiktoken for token-accurate counting
try:
    import tiktoken as _tiktoken
    _tiktoken_enc = _tiktoken.encoding_for_model("gpt-3.5-turbo")
except Exception:
    _tiktoken_enc = None  # type: ignore


def _count_tokens_heuristic(text: str) -> int:
    if _tiktoken_enc is not None:
        try:
            return len(_tiktoken_enc.encode(str(text)))
        except Exception:
            pass
    # Fallback heuristic: chars/4 with min 1
    return max(1, len(text) // 4)


class RetrievalExecutor:
    """Retrieval executor that wraps PlanExecutor functionality for backward compatibility"""
    def __init__(self):
        self.plan_executor = PlanExecutor()
    
    async def execute(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Async execute method that matches the expected interface"""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.plan_executor.execute_plan, plan)
        return result
    
    async def execute_strategy(self, strategy: Dict[str, Any], query: str) -> List[Dict[str, Any]]:
        """Execute a strategy and return the result list"""
        plan = {
            "strategy": strategy.get("strategy", "hybrid_fallback"),
            "query": query,
        }
        result = await self.execute(plan)
        return result.get("result", [])


class PlanExecutor:
    def __init__(self, policy: Optional[RoutingPolicy] = None):
        self.surreal_url = os.getenv("SURREALDB_URL", "http://127.0.0.1:8000/sql")
        self.auth = (os.getenv("SURREALDB_USER", "root"), os.getenv("SURREALDB_PASS", "root"))
        self.ns = os.getenv("SURREALDB_NS", "strata")  # Updated to match the database we created
        self.db = os.getenv("SURREALDB_DB", "strata")  # Updated to match the database we created
        self.temporal_weight = 0.3  # Weight for temporal relevance
        self.semantic_weight = 0.5  # Weight for semantic similarity
        self.keyword_weight = 0.2   # Weight for keyword matching (BM25)
        self.embedding_service = get_embedding_service()
        self.policy = policy or RoutingPolicy()

    def execute_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a query plan based on the strategy
        """
        strategy = plan.get('strategy', 'hybrid_fallback')
        query = plan.get('query', '') or ''
        
        start_time = time.time()
        
        if strategy == "event_log_first":
            result = self._execute_event_log_first(query)
        elif strategy == "knowledge_graph_first":
            result = self._execute_knowledge_graph_first(query)
        elif strategy == "hybrid_with_graph_expansion":
            result = self._execute_hybrid_with_graph_expansion(query)
        elif strategy == "composite_kg_vector":
            result = self._execute_composite_kg_vector(query)
        elif strategy == "knowledge_graph_with_invalidation":
            result = self._execute_knowledge_graph_with_invalidation(query)
        elif strategy == "hybrid_bm25_vector_temporal":
            result = self._execute_hybrid_bm25_vector_temporal(query)
        else:
            result = self._execute_hybrid_fallback(query)
        
        execution_time = time.time() - start_time
        
        output: Dict[str, Any] = {
            'strategy': strategy,
            'query': query,
            'result': result,
            'execution_time': execution_time,
            'timestamp': time.time()
        }
        
        return output

    def _execute_event_log_first(self, query: str) -> Dict[str, Any]:
        """Execute event log first strategy"""
        try:
            escaped_query = query.replace("'", "\\'")
            if not escaped_query.strip():
                sql = "SELECT * FROM event WHERE (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
            else:
                sql = f"SELECT * FROM event WHERE (content @@ '{escaped_query}' OR content CONTAINS '{escaped_query}') AND (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
            raw = self._query_surreal(sql)
            if isinstance(raw, dict):
                inner = raw.get('result', [])
                if not isinstance(inner, list):
                    inner = [inner]
                return {'events': inner, 'strategy': 'event_log_first'}
            return {'events': raw if isinstance(raw, list) else [], 'strategy': 'event_log_first'}
        except Exception as e:
            return {'error': str(e), 'strategy': 'event_log_first'}

    def _execute_knowledge_graph_first(self, query: str) -> Dict[str, Any]:
        """Execute knowledge graph first strategy, with fallback to events"""
        try:
            escaped_query = query.replace("'", "\\'")
            # 1. Try to find entities matching the query
            sql = f"SELECT * FROM entity WHERE (name @@ '{escaped_query}' OR name CONTAINS '{escaped_query}' OR '{escaped_query}' CONTAINS name) AND (forgotten IS NONE OR forgotten = false) LIMIT 10;"
            raw = self._query_surreal(sql)
            entities = []
            if isinstance(raw, dict):
                entities = raw.get('result', [])
                if not isinstance(entities, list):
                    entities = [entities]
            
            # 2. If entities found, also get their facts
            facts = []
            if entities:
                entity_ids = [e['id'] for e in entities if 'id' in e]
                if entity_ids:
                    ids_str = ", ".join([f"{eid}" for eid in entity_ids])
                    facts_sql = f"SELECT * FROM fact WHERE (in IN [{ids_str}] OR out IN [{ids_str}]) AND (valid_until IS NONE OR valid_until > time::now()) AND (forgotten IS NONE OR forgotten = false) LIMIT 20 FETCH in, out;"
                    facts_raw = self._query_surreal(facts_sql)
                    if isinstance(facts_raw, dict):
                        facts = facts_raw.get('result', [])
                        if not isinstance(facts, list):
                            facts = [facts]

            # 3. Fallback: If no facts or entities found, try a quick event search
            events = []
            if not entities and not facts:
                event_results = self._execute_hybrid_fallback(query)
                events = event_results.get('events', [])

            return {
                'entities': entities,
                'facts': facts,
                'events': events,
                'strategy': 'knowledge_graph_first'
            }
        except Exception as e:
            return {'error': str(e), 'strategy': 'knowledge_graph_first'}

    def _execute_hybrid_with_graph_expansion(self, query: str) -> Dict[str, Any]:
        """Execute hybrid strategy with graph expansion"""
        try:
            escaped_query = query.replace("'", "\\'")
            if not escaped_query.strip():
                events_sql = "SELECT * FROM event WHERE (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 5;"
                entities_sql = "SELECT * FROM entity WHERE (forgotten IS NONE OR forgotten = false) LIMIT 5;"
            else:
                events_sql = f"SELECT * FROM event WHERE (content @@ '{escaped_query}' OR content CONTAINS '{escaped_query}') AND (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 5;"
                entities_sql = f"SELECT * FROM entity WHERE (name @@ '{escaped_query}' OR name CONTAINS '{escaped_query}') AND (forgotten IS NONE OR forgotten = false) LIMIT 5;"
            
            events_result = self._query_surreal(events_sql)
            entities_result = self._query_surreal(entities_sql)
            
            return {
                'events': events_result.get('result', []) if isinstance(events_result, dict) else [],
                'entities': entities_result.get('result', []) if isinstance(entities_result, dict) else [],
                'strategy': 'hybrid_with_graph_expansion'
            }
        except Exception as e:
            return {'error': str(e), 'strategy': 'hybrid_with_graph_expansion'}

    def _execute_composite_kg_vector(self, query: str) -> Dict[str, Any]:
        """Execute composite knowledge graph and vector strategy"""
        try:
            escaped_query = query.replace("'", "\\'")
            if not escaped_query.strip():
                sql = "SELECT * FROM event WHERE (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
            else:
                sql = f"SELECT * FROM event WHERE (content @@ '{escaped_query}' OR content CONTAINS '{escaped_query}') AND (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
            raw = self._query_surreal(sql)
            if isinstance(raw, dict):
                inner = raw.get('result', [])
                if not isinstance(inner, list):
                    inner = [inner]
                return {'events': inner, 'strategy': 'composite_kg_vector'}
            return {'events': raw if isinstance(raw, list) else [], 'strategy': 'composite_kg_vector'}
        except Exception as e:
            return {'error': str(e), 'strategy': 'composite_kg_vector'}

    def _execute_knowledge_graph_with_invalidation(self, query: str) -> Dict[str, Any]:
        """Execute knowledge graph with invalidation strategy"""
        try:
            escaped_query = query.replace("'", "\\'")
            sql = f"SELECT * FROM entity WHERE (name @@ '{escaped_query}' OR name CONTAINS '{escaped_query}') AND (valid_until IS NONE OR valid_until > time::now()) AND (forgotten IS NONE OR forgotten = false) LIMIT 10;"
            raw = self._query_surreal(sql)
            if isinstance(raw, dict):
                inner = raw.get('result', [])
                if not isinstance(inner, list):
                    inner = [inner]
                return {'entities': inner, 'strategy': 'knowledge_graph_with_invalidation'}
            return {'entities': raw if isinstance(raw, list) else [], 'strategy': 'knowledge_graph_with_invalidation'}
        except Exception as e:
            return {'error': str(e), 'strategy': 'knowledge_graph_with_invalidation'}

    def _execute_hybrid_fallback(self, query: str) -> Dict[str, Any]:
        """Execute hybrid fallback strategy using both keyword and vector search, and entities"""
        try:
            # 1. Keyword search in events
            keyword_results = self._execute_bm25_search(query)
            
            # 2. Vector search in events
            vector_results = []
            if query.strip():
                vector_results = self._execute_vector_search(query)
            
            # 3. Entity search
            entities = []
            escaped_query = query.replace("'", "\\'").lower()
            if escaped_query.strip():
                # Use case-insensitive search for entity names
                sql = f"SELECT * FROM entity WHERE (string::lowercase(name) CONTAINS '{escaped_query}' OR '{escaped_query}' CONTAINS string::lowercase(name)) AND (forgotten = NONE OR forgotten = false) LIMIT 5;"
                res = self._query_surreal(sql)
                if isinstance(res, dict):
                    entities = res.get('result', [])
                    if not isinstance(entities, list):
                        entities = [entities]

            # 4. Fact search (if entities found)
            facts = []
            if entities:
                entity_ids = [e['id'] for e in entities if 'id' in e]
                if entity_ids:
                    ids_str = ", ".join([f"{eid}" for eid in entity_ids])
                    facts_sql = f"SELECT * FROM fact WHERE (in IN [{ids_str}] OR out IN [{ids_str}]) AND (valid_until = NONE OR valid_until > time::now()) AND (forgotten = NONE OR forgotten = false) LIMIT 10 FETCH in, out;"
                    facts_raw = self._query_surreal(facts_sql)
                    if isinstance(facts_raw, dict):
                        facts = facts_raw.get('result', [])
                        if not isinstance(facts, list):
                            facts = [facts]

            # 5. Combine
            seen_ids = set()
            combined = []
            
            # Add entities first
            for r in entities:
                rid = r.get('id')
                if rid and rid not in seen_ids:
                    combined.append(r)
                    seen_ids.add(rid)

            # Add facts
            for r in facts:
                rid = r.get('id')
                if rid and rid not in seen_ids:
                    combined.append(r)
                    seen_ids.add(rid)

            for r in keyword_results:
                rid = r.get('id')
                if rid and rid not in seen_ids:
                    combined.append(r)
                    seen_ids.add(rid)
            
            for r in vector_results:
                rid = r.get('id')
                if rid and rid not in seen_ids:
                    # Filter low similarity vector results in fallback
                    if r.get('similarity_score', 0) > 0.4:
                        combined.append(r)
                        seen_ids.add(rid)
            
            return {'events': combined, 'strategy': 'hybrid_fallback'}
        except Exception as e:
            return {'error': str(e), 'strategy': 'hybrid_fallback'}

    def _execute_hybrid_bm25_vector_temporal(self, query: str) -> Dict[str, Any]:
        """Execute full hybrid search combining BM25, vector similarity, and temporal relevance"""
        try:
            # Step 1: Get keyword-based results (BM25-like)
            keyword_results = self._execute_bm25_search(query)
            
            # Step 2: Get vector similarity results
            vector_results = self._execute_vector_search(query)
            
            # Step 3: Get temporal relevance results
            temporal_results = self._execute_temporal_search(query)
            
            # Step 4: Combine results with weighted scoring
            combined_events = self._combine_hybrid_results(keyword_results, vector_results, temporal_results)
            
            # Step 5: For factual queries, also check the Knowledge Graph
            entities = []
            facts = []
            
            escaped_query = query.replace("'", "\\'").lower()
            if escaped_query.strip():
                # Search for entities using case-insensitive match
                entity_sql = f"SELECT * FROM entity WHERE (string::lowercase(name) CONTAINS '{escaped_query}' OR '{escaped_query}' CONTAINS string::lowercase(name)) AND (forgotten = NONE OR forgotten = false) LIMIT 5;"
                entity_raw = self._query_surreal(entity_sql)
                if isinstance(entity_raw, dict):
                    entities = entity_raw.get('result', [])
                    if not isinstance(entities, list):
                        entities = [entities]
                
                # If entities found, get their facts
                if entities:
                    entity_ids = [e['id'] for e in entities if 'id' in e]
                    if entity_ids:
                        # Ensure we handle record IDs correctly in the query
                        ids_str = ", ".join([f"{eid}" for eid in entity_ids])
                        facts_sql = f"SELECT * FROM fact WHERE (in IN [{ids_str}] OR out IN [{ids_str}]) AND (valid_until = NONE OR valid_until > time::now()) AND (forgotten = NONE OR forgotten = false) LIMIT 10 FETCH in, out;"
                        facts_raw = self._query_surreal(facts_sql)
                        if isinstance(facts_raw, dict):
                            # Facts are returned in the 'result' field of the response dict
                            fetched_facts = facts_raw.get('result', [])
                            if not isinstance(fetched_facts, list):
                                fetched_facts = [fetched_facts]
                            facts = fetched_facts
            
            # Merge all into a flat list for the server to process
            final_result = combined_events + entities + facts
            
            return {
                'result': final_result,
                'keyword_results_count': len(keyword_results),
                'vector_results_count': len(vector_results),
                'temporal_results_count': len(temporal_results),
                'entities_count': len(entities),
                'facts_count': len(facts),
                'strategy': 'hybrid_bm25_vector_temporal'
            }
        except Exception as e:
            return {'error': str(e), 'strategy': 'hybrid_bm25_vector_temporal'}

    def _execute_bm25_search(self, query: str) -> List[Dict[str, Any]]:
        """Execute keyword-based search using SurrealDB's full-text search (BM25 equivalent)"""
        try:
            escaped_query = query.replace("'", "\\'")
            if not escaped_query.strip():
                sql = "SELECT *, search::score(1.0) AS relevance_score FROM event WHERE (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
            else:
                # Try full-text search first, fallback to CONTAINS if it fails or returns nothing
                sql = f"SELECT *, search::score(1.0) AS relevance_score FROM event WHERE content @@ '{escaped_query}' AND (forgotten IS NONE OR forgotten = false) ORDER BY relevance_score DESC LIMIT 10;"
            result = self._query_surreal(sql)
            
            items = []
            if isinstance(result, dict) and 'result' in result:
                items = result['result']
            
            if not items:
                # Fallback to simple CONTAINS search
                sql = f"SELECT * FROM event WHERE content CONTAINS '{escaped_query}' AND (forgotten IS NONE OR forgotten = false) LIMIT 10;"
                result = self._query_surreal(sql)
                if isinstance(result, dict) and 'result' in result:
                    items = result['result']
                    for item in items:
                        item['relevance_score'] = 0.5 # Default score for fallback
            
            return items
        except Exception as e:
            print(f"BM25 search error: {e}")
            return []

    def _execute_vector_search(self, query: str) -> List[Dict[str, Any]]:
        """Execute vector similarity search using SurrealDB's native vector functions"""
        try:
            # Get query embedding
            query_vector = self.embedding_service.embed_for_query(query)
            query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"
            
            # Use SurrealDB's native vector::similarity::cosine function
            # We add a dimension check to avoid errors with mixed models
            sql = f"""
            SELECT id, content, vector::similarity::cosine(embedding, {query_vector_str}) AS similarity_score 
            FROM event 
            WHERE embedding IS NOT NONE 
              AND (forgotten IS NONE OR forgotten = false)
              AND array::len(embedding) = {len(query_vector)}
            ORDER BY similarity_score DESC 
            LIMIT 10;
            """
            result = self._query_surreal(sql)
            
            if isinstance(result, dict) and 'result' in result:
                return result['result']
            return []
            
        except Exception as e:
            print(f"Vector search error: {e}")
            return []

    def _execute_temporal_search(self, query: str) -> List[Dict[str, Any]]:
        """Execute temporal relevance search based on time proximity"""
        try:
            escaped_query = query.replace("'", "\\'")
            # We want recent events, regardless of content match if query is temporal? 
            # Actually, usually we still want some relevance.
            if not escaped_query.strip():
                sql = "SELECT *, (time::now() - timestamp) AS time_diff_duration FROM event WHERE (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
            else:
                sql = f"SELECT *, (time::now() - timestamp) AS time_diff_duration FROM event WHERE (content @@ '{escaped_query}' OR content CONTAINS '{escaped_query}') AND (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
            result = self._query_surreal(sql)
            
            if isinstance(result, dict) and 'result' in result:
                events = result['result']
                now = datetime.now(timezone.utc)
                for event in events:
                    ts = event.get('timestamp')
                    if isinstance(ts, str):
                        try:
                            # Parse ISO timestamp
                            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                            # Use exponential decay: score = exp(-age_in_days / 30)
                            # This gives a score between 0 and 1 that decays over time
                            age_seconds = (now - dt).total_seconds()
                            age_days = max(0, age_seconds / (24 * 3600))
                            event['temporal_score'] = float(np.exp(-age_days / 30))
                        except Exception:
                            event['temporal_score'] = 0.1
                    else:
                        event['temporal_score'] = 0.1
                return events
            else:
                return []
        except Exception as e:
            print(f"Temporal search error: {e}")
            return []

    def _calculate_similarity_score(self, query: str, content: str) -> float:
        """Calculate a basic similarity score between query and content"""
        query_words = set(query.lower().split())
        content_words = set(content.lower().split())
        intersection = query_words.intersection(content_words)
        union = query_words.union(content_words)
        if len(union) == 0:
            return 0.0
        return len(intersection) / len(union)  # Jaccard similarity

    def _combine_hybrid_results(self, keyword_results: List[Dict], vector_results: List[Dict], temporal_results: List[Dict]) -> List[Dict]:
        """Combine results from different search modalities with weighted scoring"""
        # Create a map of event IDs to their scores from different modalities
        combined_scores = {}
        result_map = {}
        
        # Add keyword scores (BM25)
        for i, result in enumerate(keyword_results):
            event_id = result.get('id', f'kw_{i}')
            combined_scores[event_id] = {
                'keyword_score': result.get('relevance_score', 0.0),
                'vector_score': 0.0,
                'temporal_score': 0.0
            }
            result_map[event_id] = result
        
        # Add vector scores
        for i, result in enumerate(vector_results):
            event_id = result.get('id', f'vec_{i}')
            if event_id not in combined_scores:
                combined_scores[event_id] = {
                    'keyword_score': 0.0,
                    'vector_score': 0.0,
                    'temporal_score': 0.0
                }
                result_map[event_id] = result
            combined_scores[event_id]['vector_score'] = result.get('similarity_score', 0.0)
            # Update result_map if not already present
            if event_id not in result_map:
                result_map[event_id] = result
        
        # Add temporal scores
        for i, result in enumerate(temporal_results):
            event_id = result.get('id', f'temp_{i}')
            if event_id not in combined_scores:
                combined_scores[event_id] = {
                    'keyword_score': 0.0,
                    'vector_score': 0.0,
                    'temporal_score': 0.0
                }
                result_map[event_id] = result
            combined_scores[event_id]['temporal_score'] = result.get('temporal_score', 0.0)
            # Update result_map if not already present
            if event_id not in result_map:
                result_map[event_id] = result

        # Calculate final hybrid score for each result
        final_results = []
        for event_id, scores in combined_scores.items():
            hybrid_score = (
                self.keyword_weight * scores['keyword_score'] +
                self.semantic_weight * scores['vector_score'] +
                self.temporal_weight * scores['temporal_score']
            )
            final_result = result_map[event_id].copy()
            final_result['hybrid_score'] = hybrid_score
            final_results.append(final_result)

        # Sort by hybrid score in descending order
        final_results.sort(key=lambda x: x.get('hybrid_score', 0.0), reverse=True)
        return final_results

    def _query_surreal(self, sql: str) -> Dict[str, Any]:
        """Execute a SurrealDB query"""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        full_sql = f"USE NS {self.ns} DB {self.db};\n{sql}"
        
        response = requests.post(
            self.surreal_url,
            data=full_sql,
            headers=headers,
            auth=self.auth,
            timeout=30
        )
        
        result = response.json()
        if isinstance(result, list) and len(result) > 1 and 'result' in result[1]:
            return result[1]
        elif isinstance(result, list) and len(result) > 0 and 'result' in result[0]:
            return result[0]
        return {'result': result if isinstance(result, list) else [result]}

use crate::surreal::SurrealClient;

pub struct Executor {
    db: SurrealClient,
}

impl Executor {
    pub fn new() -> Self {
        Self {
            db: SurrealClient::new(),
        }
    }

    pub async fn execute(&self, plan: crate::plan_builder::Plan) -> serde_json::Value {
        let mut results = Vec::new();
        
        for step in plan.steps {
            let step_result = match step {
                crate::plan_builder::PlanStep::SelectFromEventLog => {
                    self.select_from_event_log(&plan.query).await
                },
                crate::plan_builder::PlanStep::SelectFromKnowledgeGraph => {
                    self.select_from_knowledge_graph(&plan.query).await
                },
                crate::plan_builder::PlanStep::ExpandGraphRelations => {
                    self.expand_graph_relations(&plan.query).await
                },
                crate::plan_builder::PlanStep::SelectFromEventLogWithEmbedding => {
                    self.select_from_event_log_with_embedding(&plan.query).await
                },
                crate::plan_builder::PlanStep::SimilaritySearch => {
                    self.similarity_search(&plan.query).await
                },
                crate::plan_builder::PlanStep::CheckValidity => {
                    self.check_validity(&plan.query).await
                },
                crate::plan_builder::PlanStep::HybridSearch => {
                    self.hybrid_search(&plan.query).await
                },
            };
            results.push(step_result);
        }

        // Combine results based on strategy
        self.combine_results(&plan.strategy, results)
    }

    // Helper function to combine results based on strategy
    fn combine_results(&self, strategy: &str, mut results: Vec<serde_json::Value>) -> serde_json::Value {
        if results.is_empty() {
            return serde_json::json!({
                "status": "ok",
                "strategy": strategy,
                "results": []
            });
        }

        // For single result strategies, return the first result
        if results.len() == 1 {
            return serde_json::json!({
                "status": "ok",
                "strategy": strategy,
                "results": results[0]
            });
        }

        // For multi-step strategies, combine results appropriately
        match strategy {
            "hybrid_with_graph_expansion" => {
                // Expecting two results: events and entities
                let events = if results.len() > 0 {
                    results.remove(0).get("results").cloned().unwrap_or_else(|| serde_json::json!([]))
                } else {
                    serde_json::json!([])
                };
                
                let entities = if !results.is_empty() {
                    results.remove(0).get("results").cloned().unwrap_or_else(|| serde_json::json!([]))
                } else {
                    serde_json::json!([])
                };

                serde_json::json!({
                    "status": "ok",
                    "strategy": strategy,
                    "events": events,
                    "entities": entities,
                    "relations": [] // Will be filled by expand_graph_relations
                })
            },
            "hybrid_bm25_vector_temporal" => {
                // Expecting three results: keyword, vector, temporal
                let keyword_results = if results.len() > 0 {
                    results.remove(0).get("results").cloned().unwrap_or_else(|| serde_json::json!([]))
                } else {
                    serde_json::json!([])
                };
                
                let vector_results = if !results.is_empty() {
                    results.remove(0).get("results").cloned().unwrap_or_else(|| serde_json::json!([]))
                } else {
                    serde_json::json!([])
                };
                
                let temporal_results = if !results.is_empty() {
                    results.remove(0).get("results").cloned().unwrap_or_else(|| serde_json::json!([]))
                } else {
                    serde_json::json!([])
                };

                serde_json::json!({
                    "status": "ok",
                    "strategy": strategy,
                    "keyword_results": keyword_results,
                    "vector_results": vector_results,
                    "temporal_results": temporal_results,
                    "combined_results": self.combine_hybrid_results(&keyword_results, &vector_results, &temporal_results)
                })
            },
            _ => {
                // For other multi-step strategies, return all results
                serde_json::json!({
                    "status": "ok",
                    "strategy": strategy,
                    "results": results
                })
            }
        }
    }

    async fn select_from_event_log(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        let surql = if escaped_query.trim().is_empty() {
            "SELECT * FROM event WHERE (forgotten IS NULL OR forgotten = false) ORDER BY timestamp DESC LIMIT 10".to_string()
        } else {
            format!(
                "SELECT * FROM event WHERE (content @@ '{}' OR content CONTAINS '{}') AND (forgotten IS NULL OR forgotten = false) ORDER BY timestamp DESC LIMIT 10",
                escaped_query, escaped_query
            )
        };

        match self.db.query(&surql).await {
            Ok(response) => {
                // Extract the actual results from the response
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                
                serde_json::json!({
                    "status": "ok",
                    "step": "select_from_event_log",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "select_from_event_log",
                "error": err.to_string()
            }),
        }
    }

    async fn select_from_knowledge_graph(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        let surql = if escaped_query.trim().is_empty() {
            "SELECT * FROM entity WHERE (forgotten IS NULL OR forgotten = false) LIMIT 10".to_string()
        } else {
            format!(
                "SELECT * FROM entity WHERE (name @@ '{0}' OR name CONTAINS '{0}' OR '{0}' CONTAINS name) AND (forgotten IS NULL OR forgotten = false) LIMIT 10",
                escaped_query
            )
        };

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "select_from_knowledge_graph",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "select_from_knowledge_graph",
                "error": err.to_string()
            }),
        }
    }

    async fn expand_graph_relations(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        // This would involve more complex queries to expand relations
        let surql = format!(
            "SELECT ->fact->entity FROM entity WHERE name @@ '{}' AND (forgotten IS NULL OR forgotten = false) LIMIT 5",
            escaped_query
        );

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "expand_graph_relations",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "expand_graph_relations",
                "error": err.to_string()
            }),
        }
    }

    async fn select_from_event_log_with_embedding(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        let surql = format!(
            "SELECT * FROM event WHERE content @@ '{}' AND (forgotten IS NULL OR forgotten = false) AND array::len(embedding) = 384 ORDER BY timestamp DESC LIMIT 10",
            escaped_query
        );

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "select_from_event_log_with_embedding",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "select_from_event_log_with_embedding",
                "error": err.to_string()
            }),
        }
    }

    async fn similarity_search(&self, query: &str) -> serde_json::Value {
        // Placeholder for vector similarity search - implementation depends on SurrealDB 3+ native vector capabilities
        let escaped_query = query.replace("'", "''");
        
        // We simulate the native vector search if possible, or fallback to keyword + forgotten filter
        let surql = format!(
            "SELECT *, vector::similarity::cosine(embedding, (SELECT embedding FROM event WHERE content @@ '{}' LIMIT 1).embedding) AS score 
             FROM event 
             WHERE (forgotten IS NULL OR forgotten = false) 
               AND array::len(embedding) = 384 
             ORDER BY score DESC LIMIT 5",
            escaped_query
        );

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "similarity_search",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "similarity_search",
                "error": err.to_string()
            }),
        }
    }

    async fn check_validity(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        // Check for validity of facts (valid_until field or similar)
        let surql = if escaped_query.trim().is_empty() {
             "SELECT * FROM entity WHERE (valid_until IS NULL OR valid_until > time::now()) AND (forgotten IS NULL OR forgotten = false) LIMIT 10".to_string()
        } else {
             format!(
                "SELECT * FROM entity WHERE (name @@ '{0}' OR name CONTAINS '{0}') AND (valid_until IS NULL OR valid_until > time::now()) AND (forgotten IS NULL OR forgotten = false) LIMIT 10",
                escaped_query
            )
        };

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "check_validity",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "check_validity",
                "error": err.to_string()
            }),
        }
    }

    async fn hybrid_search(&self, query: &str) -> serde_json::Value {
        // Execute the three different search modalities
        let keyword_results = self.execute_bm25_search(query).await;
        let vector_results = self.execute_vector_search(query).await;
        let temporal_results = self.execute_temporal_search(query).await;

        serde_json::json!({
            "status": "ok",
            "step": "hybrid_search",
            "keyword_results": keyword_results,
            "vector_results": vector_results,
            "temporal_results": temporal_results,
            "combined_results": self.combine_hybrid_results(
                &keyword_results,
                &vector_results,
                &temporal_results
            )
        })
    }

    async fn execute_bm25_search(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        // Use SurrealDB's full-text search with scoring
        let surql = if escaped_query.trim().is_empty() {
             "SELECT *, search::score(1.0) AS relevance_score FROM event WHERE (forgotten IS NULL OR forgotten = false) ORDER BY timestamp DESC LIMIT 10".to_string()
        } else {
             format!(
                "SELECT *, search::score(1.0) AS relevance_score FROM event WHERE content @@ '{}' AND (forgotten IS NULL OR forgotten = false) ORDER BY relevance_score DESC LIMIT 10",
                escaped_query
            )
        };

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "bm25_search",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "bm25_search",
                "error": err.to_string()
            }),
        }
    }

    async fn execute_vector_search(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        // Use basic full-text search as fallback since native vector search requires embeddings from Python
        let surql = if escaped_query.trim().is_empty() {
             "SELECT * FROM event WHERE (forgotten IS NULL OR forgotten = false) ORDER BY timestamp DESC LIMIT 10".to_string()
        } else {
             format!(
                "SELECT * FROM event WHERE (content @@ '{0}' OR content CONTAINS '{0}') AND (forgotten IS NULL OR forgotten = false) ORDER BY timestamp DESC LIMIT 10",
                escaped_query
            )
        };

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "vector_search",
                    "results": results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "vector_search",
                "error": err.to_string()
            }),
        }
    }

    async fn execute_temporal_search(&self, query: &str) -> serde_json::Value {
        let escaped_query = query.replace("'", "''");
        // Search with temporal relevance
        let surql = if escaped_query.trim().is_empty() {
             "SELECT *, time::now() - timestamp AS time_diff FROM event WHERE (forgotten IS NULL OR forgotten = false) ORDER BY timestamp DESC LIMIT 10".to_string()
        } else {
             format!(
                "SELECT *, time::now() - timestamp AS time_diff FROM event WHERE (content @@ '{0}' OR content CONTAINS '{0}') AND (forgotten IS NULL OR forgotten = false) ORDER BY timestamp DESC LIMIT 10",
                escaped_query
            )
        };

        match self.db.query(&surql).await {
            Ok(response) => {
                let results = response.as_array()
                    .and_then(|arr| arr.get(1))
                    .and_then(|obj| obj.get("result"))
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!([]));
                    
                // Add temporal scoring to results
                let scored_results = results.as_array().unwrap_or(&Vec::new()).iter()
                    .map(|item| {
                        let mut scored_item = item.clone();
                        if let Some(time_diff) = item.get("time_diff") {
                            // Calculate temporal score (higher for more recent)
                            let temp_score = 1.0 / (1.0 + time_diff.as_f64().unwrap_or(0.0));
                            scored_item["temporal_score"] = serde_json::Value::Number(serde_json::Number::from_f64(temp_score).unwrap());
                        } else {
                            scored_item["temporal_score"] = serde_json::Value::Number(serde_json::Number::from_f64(0.0).unwrap());
                        }
                        scored_item
                    })
                    .collect::<Vec<_>>();
                    
                serde_json::json!({
                    "status": "ok",
                    "step": "temporal_search",
                    "results": scored_results
                })
            }
            Err(err) => serde_json::json!({
                "status": "error",
                "step": "temporal_search",
                "error": err.to_string()
            }),
        }
    }

    fn combine_hybrid_results(
        &self,
        keyword_results: &serde_json::Value,
        vector_results: &serde_json::Value,
        temporal_results: &serde_json::Value
    ) -> serde_json::Value {
        // Create a map of event IDs to their scores from different modalities
        let mut combined_scores = std::collections::HashMap::new();
        let mut result_map = std::collections::HashMap::new();

        // Process keyword results
        if let Some(kw_array) = keyword_results.as_array() {
            for (i, result) in kw_array.iter().enumerate() {
                let event_id = result.get("id").map(|id| id.to_string()).unwrap_or_else(|| format!("kw_{}", i));
                let mut scores = std::collections::HashMap::new();
                scores.insert("keyword_score", result.get("relevance_score").and_then(|v| v.as_f64()).unwrap_or(0.0));
                scores.insert("vector_score", 0.0);
                scores.insert("temporal_score", 0.0);
                combined_scores.insert(event_id.clone(), scores);
                result_map.insert(event_id, result.clone());
            }
        }

        // Process vector results
        if let Some(vec_array) = vector_results.as_array() {
            for (i, result) in vec_array.iter().enumerate() {
                let event_id = result.get("id").map(|id| id.to_string()).unwrap_or_else(|| format!("vec_{}", i));
                let scores = combined_scores.entry(event_id.clone()).or_insert_with(|| {
                    std::collections::HashMap::new()
                });
                *scores.entry("vector_score").or_insert(0.0) = result.get("similarity_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
                if !result_map.contains_key(&event_id) {
                    result_map.insert(event_id.clone(), result.clone());
                }
            }
        }

        // Process temporal results
        if let Some(temp_array) = temporal_results.as_array() {
            for (i, result) in temp_array.iter().enumerate() {
                let event_id = result.get("id").map(|id| id.to_string()).unwrap_or_else(|| format!("temp_{}", i));
                let scores = combined_scores.entry(event_id.clone()).or_insert_with(|| {
                    std::collections::HashMap::new()
                });
                *scores.entry("temporal_score").or_insert(0.0) = result.get("temporal_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
                if !result_map.contains_key(&event_id) {
                    result_map.insert(event_id.clone(), result.clone());
                }
            }
        }

        // Calculate final hybrid score for each result
        let mut final_results = Vec::new();
        for (event_id, scores) in combined_scores {
            let keyword_score = *scores.get("keyword_score").unwrap_or(&0.0);
            let vector_score = *scores.get("vector_score").unwrap_or(&0.0);
            let temporal_score = *scores.get("temporal_score").unwrap_or(&0.0);
            
            // Calculate weighted hybrid score
            let hybrid_score = 0.2 * keyword_score + 0.5 * vector_score + 0.3 * temporal_score; // Weights for keyword, vector, temporal
            
            let mut final_result = result_map.get(&event_id).unwrap_or(&serde_json::Value::Null).clone();
            if !final_result.is_null() {
                final_result["hybrid_score"] = serde_json::Value::Number(serde_json::Number::from_f64(hybrid_score).unwrap());
                final_results.push(final_result);
            }
        }

        // Sort by hybrid score in descending order
        final_results.sort_by(|a, b| {
            let score_a = a.get("hybrid_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let score_b = b.get("hybrid_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            score_b.partial_cmp(&score_a).unwrap_or(std::cmp::Ordering::Equal)
        });

        serde_json::Value::Array(final_results)
    }
}
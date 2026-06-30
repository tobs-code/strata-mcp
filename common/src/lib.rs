use serde::{Deserialize, Serialize};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use std::sync::atomic::{AtomicU32, Ordering};

// Global health factor (0.1 - 1.0), scaled by 100 for atomic storage
static HEALTH_FACTOR: AtomicU32 = AtomicU32::new(100);

pub fn update_system_health(factor: f64) {
    let scaled = (factor.max(0.1).min(1.0) * 100.0) as u32;
    HEALTH_FACTOR.store(scaled, Ordering::Relaxed);
}

pub fn get_system_health() -> f64 {
    HEALTH_FACTOR.load(Ordering::Relaxed) as f64 / 100.0
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryClassification {
    pub query_type: QueryType,
    pub confidence: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum QueryType {
    #[serde(rename = "temporal")]
    Temporal,
    #[serde(rename = "factual")]
    Factual,
    #[serde(rename = "multi-hop")]
    MultiHop,
    #[serde(rename = "conversational")]
    Conversational,
    #[serde(rename = "update")]
    Update,
}

impl std::fmt::Display for QueryType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            QueryType::Temporal => write!(f, "temporal"),
            QueryType::Factual => write!(f, "factual"),
            QueryType::MultiHop => write!(f, "multi-hop"),
            QueryType::Conversational => write!(f, "conversational"),
            QueryType::Update => write!(f, "update"),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Strategy {
    pub name: String,
    pub cost_budget: CostBudget,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Copy)]
pub enum CostBudget {
    #[serde(rename = "low")]
    Low,
    #[serde(rename = "medium")]
    Medium,
    #[serde(rename = "high")]
    High,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BudgetTracker {
    pub budget: CostBudget,
    pub db_calls: u32,
    pub estimated_tokens: u32,
    pub start_time: f64,
    pub end_time: Option<f64>,
}

impl BudgetTracker {
    pub fn new(budget: CostBudget) -> Self {
        let start = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::from_secs(0))
            .as_secs_f64();
            
        Self {
            budget,
            db_calls: 0,
            estimated_tokens: 0,
            start_time: start,
            end_time: None,
        }
    }

    pub fn record_db_call(&mut self, count: u32) {
        self.db_calls += count;
    }

    pub fn record_tokens(&mut self, count: u32) {
        self.estimated_tokens += count;
    }

    pub fn finish(&mut self) {
        self.end_time = Some(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or(Duration::from_secs(0))
                .as_secs_f64()
        );
    }

    pub fn is_over_budget(&self) -> bool {
        let (base_db, base_tokens) = match self.budget {
            CostBudget::Low => (10, 1000),
            CostBudget::Medium => (25, 3000),
            CostBudget::High => (50, 8000),
        };
        
        let health = get_system_health();
        let max_db = (base_db as f64 * health) as u32;
        let max_tokens = (base_tokens as f64 * health) as u32;
        
        self.db_calls > max_db || self.estimated_tokens > max_tokens
    }

    pub fn duration_seconds(&self) -> f64 {
        match self.end_time {
            Some(end) => end - self.start_time,
            None => SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or(Duration::from_secs(0))
                .as_secs_f64() - self.start_time,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_health_factor_scaling() {
        // Reset to healthy
        update_system_health(1.0);
        let mut tracker = BudgetTracker::new(CostBudget::Medium);
        
        // Base limit for Medium is 25 db_calls
        tracker.record_db_call(25);
        assert!(!tracker.is_over_budget(), "Should not be over budget at 25 calls with health 1.0");
        
        tracker.record_db_call(1);
        assert!(tracker.is_over_budget(), "Should be over budget at 26 calls with health 1.0");

        // Scale health down to 0.5
        update_system_health(0.5);
        let mut degraded_tracker = BudgetTracker::new(CostBudget::Medium);
        
        // Scaled limit for Medium should now be 12 (25 * 0.5)
        degraded_tracker.record_db_call(12);
        assert!(!degraded_tracker.is_over_budget(), "Should not be over budget at 12 calls with health 0.5");
        
        degraded_tracker.record_db_call(1);
        assert!(degraded_tracker.is_over_budget(), "Should be over budget at 13 calls with health 0.5");
    }
}
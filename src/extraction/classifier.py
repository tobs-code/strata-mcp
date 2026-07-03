"""
Query Classifier (Python Implementation) for sieveon
Klassifiziert Queries in die Typen: temporal, factual, multi-hop, conversational, update
"""

import re
from typing import Dict, Tuple


class QueryClassifier:
    def __init__(self):
        # Regel-basierte Muster für schnelle Klassifikation (DE + EN)
        self.temporal_patterns = [
            # Deutsch
            r"\bwann\b",
            r"\bgestern\b",
            r"\bheute\b",
            r"\bmorgen\b",
            r"\bletzt\b",
            r"\bnächste\b",
            r"\btimestamp\b",
            r"\bzeit\b",
            r"\bdatum\b",
            r"\bseit\b",
            r"\bbis\b",
            r"\bänderung\b",
            r"\bgeändert\b",
            # Englisch
            r"\bwhen\b",
            r"\byesterday\b",
            r"\btoday\b",
            r"\btomorrow\b",
            r"\blast\b",
            r"\bnext\b",
            r"\btime\b",
            r"\bdate\b",
            r"\bsince\b",
            r"\buntil\b",
            r"\bchange\b",
            r"\bchanged\b",
        ]
        self.factual_patterns = [
            # Deutsch
            r"\bwer\b",
            r"\bwas\b",
            r"\bwelche\b",
            r"\bwo\b",
            r"\bhat\b",
            r"\bhaben\b",
            r"\bist\b",
            r"\bworan\b",
            r"\bwomit\b",
            r"\bwodurch\b",
            r"\bwerdegang\b",
            r"\bfakten\b",
            r"\binfos\b",
            r"\bnenne\b",
            r"\bliste\b",
            r"\bzeig\b",
            r"\bfinde\b",
            # Englisch
            r"\bwho\b",
            r"\bwhat\b",
            r"\bwhich\b",
            r"\bwhere\b",
            r"\bhas\b",
            r"\bhave\b",
            r"\bis\b",
            r"\blist\b",
            r"\bshow\b",
            r"\bfind\b",
            r"\btell\b",
        ]
        self.multi_hop_patterns = [
            # Deutsch
            r"\bwarum\b",
            r"\bweshalb\b",
            r"\bwieso\b",
            r"\bwegen\b",
            r"\bdaher\b",
            r"\bdeshalb\b",
            r"\bbeziehung\b",
            r"\bverbunden\b",
            r"\bzusammenhang\b",
            r"\bund wo\b",
            r"\bund was\b",
            r"\bund welche\b",
            # Englisch
            r"\bwhy\b",
            r"\bbecause\b",
            r"\breason\b",
            r"\brelation\b",
            r"\bconnected\b",
            r"\brelationship\b",
            r"\band where\b",
            r"\band what\b",
            r"\band which\b",
        ]
        self.conversational_patterns = [
            # Deutsch
            r"\bworüber\b",
            r"\büber was\b",
            r"\bgesprochen\b",
            r"\bredeten\b",
            r"\bunterhielt\b",
            r"\berinnerst du dich\b",
            r"\bweißt du noch\b",
            # Englisch
            r"\bwhat about\b",
            r"\btalked about\b",
            r"\bspoke about\b",
            r"\btalking about\b",
            r"\bremember\b",
            r"\bdo you recall\b",
        ]
        self.update_patterns = [
            # Deutsch
            r"\baktualisiere\b",
            r"\bupdate\b",
            r"\bändere\b",
            r"\bkorrigiere\b",
            r"\bsetze\b",
            r"\büberschreibe\b",
            # Englisch
            r"\bupdate\b",
            r"\bchange\b",
            r"\bmodify\b",
            r"\bcorrect\b",
            r"\bset\b",
            r"\boverwrite\b",
        ]

    def classify(self, query: str) -> Tuple[str, float]:
        """
        Klassifiziert eine Query und gibt (type, confidence) zurück.
        """
        query_lower = query.lower()
        scores: Dict[str, int] = {
            "temporal": 0,
            "factual": 0,
            "multi-hop": 0,
            "conversational": 0,
            "update": 0,
        }

        # Regel-basierte Score-Berechnung
        for pattern in self.temporal_patterns:
            if re.search(pattern, query_lower):
                scores["temporal"] += 1
        for pattern in self.factual_patterns:
            if re.search(pattern, query_lower):
                scores["factual"] += 1
        for pattern in self.multi_hop_patterns:
            if re.search(pattern, query_lower):
                scores["multi-hop"] += 1
        for pattern in self.conversational_patterns:
            if re.search(pattern, query_lower):
                scores["conversational"] += 2  # Höheres Gewicht für Conversational!
        for pattern in self.update_patterns:
            if re.search(pattern, query_lower):
                scores["update"] += 2  # Höheres Gewicht für Update!

        # Prioritäts-Liste (wichtigste → unwichtigste)
        priority_order = [
            "update",
            "multi-hop",
            "conversational",
            "temporal",
            "factual",
        ]

        # Bestes Ergebnis ermitteln (mit Priorität als Tiebreaker!)
        # Zuerst sortieren nach Score descending, dann nach Priorität!
        sorted_types = sorted(
            scores.keys(), key=lambda t: (-scores[t], priority_order.index(t))
        )
        best_type = sorted_types[0]
        best_score = scores[best_type]

        if best_score == 0:
            return "factual", 0.5  # Default, falls keine Muster passen

        # Confidence = Abstand zum Zweitplatzierten (Margin)
        sorted_scores = sorted(scores.values(), reverse=True)
        second_best_score = sorted_scores[1] if len(sorted_scores) > 1 else 0

        margin = (best_score - second_best_score) / best_score
        confidence = 0.5 + (margin * 0.5)  # Skaliert auf 0.5..1.0

        return best_type, round(confidence, 2)


# Beispiel-Nutzung
if __name__ == "__main__":
    classifier = QueryClassifier()

    test_queries = [
        "Wann habe ich Alice getroffen?",
        "Wer ist mein Kunde?",
        "Warum haben wir das Projekt gestoppt?",
        "Worüber haben wir gestern gesprochen?",
        "Aktualisiere meinen Namen auf Max.",
    ]

    print("📊 Query Classification Test:")
    for q in test_queries:
        q_type, conf = classifier.classify(q)
        print(f"  '{q}' → {q_type} (confidence: {conf:.2f})")

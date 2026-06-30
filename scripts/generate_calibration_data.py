"""
Kalibrierungs-Datengenerator für das Entropy Gate.
Erstellt 50 Einträge via EntropyGate.ingest() – genau wie das MCP-Tool memory_store.
"""
import sys
import os
import random

# Projekt-Root zum Python-Path hinzufügen
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.extraction.entropy_gate import EntropyGate

# 50 verschiedene Texte mit unterschiedlicher Länge, Entropy und Themen
TEXTE = [
    # Alltägliche, kurze Sätze (niedrige Entropy)
    "Das Wetter ist heute schön",
    "Ich habe Hunger",
    "Heute ist Montag",
    "Die Sonne scheint",
    "Ich gehe spazieren",
    "Kaffee schmeckt gut",
    "Es regnet schon wieder",
    "Heute war ein langer Tag",
    "Das Essen war lecker",
    "Ich bin müde",

    # Mittellange, alltägliche Texte
    "Ich habe heute mit meinem Team an der neuen Feature-Entwicklung gearbeitet",
    "Der Kunde hat Feedback zu unserem letzten Release gegeben",
    "Wir müssen die Tests noch fertigstellen bevor wir deployen",
    "Das Meeting wurde auf nächste Woche verschoben",
    "Ich habe den Bug in der Login-Komponente gefunden und gefixt",
    "Die CI-Pipeline läuft wieder stabil durch",
    "Wir brauchen eine bessere Dokumentation für die API",
    "Der neue Kollege fängt nächsten Monat an",
    "Die Datenbank-Migration ist abgeschlossen",
    "Ich habe das Code-Review für den Pull-Request gemacht",

    # Technische Themen (höhere Entropy)
    "Rust bietet Speichersicherheit ohne Garbage Collector durch sein Ownership-Modell mit Borrow-Checker",
    "Das Tokio-Framework ermöglicht asynchrone I/O-Operationen in Rust mit einem Work-Stealing-Scheduler",
    "SurrealDB kombiniert Graph-, Dokument- und Relationale Datenbanken in einer einzigen Engine",
    "Der MCP Server verwendet FastMCP für die Implementierung des Model Context Protocols",
    "Embedding-Vektoren werden mit cosine similarity in SurrealDB verglichen",
    "Das Entropy Gate entscheidet basierend auf Text-Entropy und Embedding-Novelty",
    "Der Circuit Breaker verhindert kaskadierende Fehler bei SurrealDB-Ausfällen",
    "Die gate_log Tabelle speichert alle Entscheidungen des Entropy Gates für die Kalibrierung",
    "Vector Indizes in SurrealDB nutzen MTREE für effiziente Ähnlichkeitssuche",
    "Das Routing Policy System klassifiziert Queries in temporal, factual, multi-hop und conversational",

    # Wissenschaftliche Themen (hohe Entropy)
    "Quantencomputer nutzen Qubits in Superposition für parallele Berechnungen in der Kryptographie",
    "Die CRISPR-Cas9 Genschere ermöglicht präzise Editierung des Erbguts in lebenden Zellen",
    "Fusionsreaktoren erzeugen Energie durch Verschmelzung von Wasserstoff-Isotopen bei extremen Temperaturen",
    "Neuronale Netze mit Transformatoren bilden die Grundlage für moderne Large Language Models",
    "Die String-Theorie postuliert 11 Dimensionen und vereinheitlicht Quantenmechanik mit Gravitation",
    "Mitochondrien produzieren ATP durch oxidative Phosphorylierung in der Atmungskette",
    "Schwarze Löcher entstehen wenn massereiche Sterne unter ihrer eigenen Gravitation kollabieren",
    "Die Blockchain-Technologie verwendet Konsensmechanismen wie Proof-of-Stake für Dezentralisierung",
    "Photovoltaik-Zellen wandeln Sonnenlicht durch den photoelektrischen Effekt in elektrische Energie um",
    "Die Evolutionstheorie basiert auf natürlicher Selektion und Mutation genetischer Informationen",

    # Gemischte Themen
    "Ich habe gestern einen Film gesehen und fand ihn sehr unterhaltsam",
    "Das neue Restaurant in der Stadt hat eine ausgezeichnete Bewertung bekommen",
    "Der Flug wurde wegen schlechten Wetters gestrichen und wir mussten umbuchen",
    "Die Mietpreise in der Innenstadt sind in den letzten Jahren stark gestiegen",
    "Ich lese gerade ein Buch über die Geschichte des Römischen Reiches",
    "Der Marathon nächstes Wochenende wurde aufgrund der Hitze abgesagt",
    "Mein Laptop ist abgestürzt und ich habe ungesicherte Arbeit verloren",
    "Die neue Version der Software hat einige wichtige Sicherheitslücken geschlossen",
    "Wir planen einen Team-Ausflug in den Kletterpark nächsten Monat",
    "Der Stromausfall letzte Nacht hat die Server beeinträchtigt",

    # Kurze, banal wirkende Texte (sollten evtl. ignoriert werden)
    "Termin um 15 Uhr",
    "Bitte ans Meeting denken",
    "Heute ist Feiertag",
    "Milch und Brot kaufen",
    "Anruf bei Mama",
    "Password zurückgesetzt",
    "Update installiert",
    "Backup läuft",
    "Logs geprüft",
    "Deployment erfolgreich",
]


def main():
    gate = EntropyGate()
    print(f"Erstelle {len(TEXTE)} Einträge via EntropyGate...")
    print(f"  Threshold: {gate.config.threshold}")
    print(f"  Alpha (Entropy): {gate.config.alpha}")
    print(f"  Beta (Novelty): {gate.config.beta}")
    print(f"  Min-Length: {gate.config.min_length}")
    print(f"  Max-Length: {gate.config.max_length}")
    print()

    stats = {"extract": 0, "ignore": 0, "skip": 0}
    
    for i, text in enumerate(TEXTE, 1):
        try:
            event_id = gate.ingest(text, source="calibration_script", debug=False)
            # Kurze Info was passiert ist
            if event_id:
                print(f"  [{i:2d}/{len(TEXTE)}] ✅ Event: {event_id[:25]}... | Text: {text[:50]}...")
            else:
                print(f"  [{i:2d}/{len(TEXTE)}] ❌ Fehler bei: {text[:50]}...")
        except Exception as e:
            print(f"  [{i:2d}/{len(TEXTE)}] ❌ Exception: {e}")

    print()
    print("Fertig! Jetzt kannst du per MCP-Tool memory_stats die gate_pass_rate prüfen.")
    print("Oder direkt in SurrealDB:")
    print("  SELECT decision, gate_score, text_score, novelty, text FROM gate_log;")


if __name__ == "__main__":
    main()
"""
Gate-Test-Injektor: Speist verschiedene Textkategorien ein um das Gate-Verhalten zu testen.
Nutzt EntropyGate.ingest() direkt – identisch zum MCP-Tool memory_store.

Kategorien:
  A) Banale Alltagstexte (sollten ignoriert werden)
  B) Banale Texte mit versteckten wichtigen Infos
  C) Normale informative Texte
  D) Sehr kurze/kryptische Notizen
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.extraction.entropy_gate import EntropyGate

TEXTE = {
    "A_banal": [
        "Heute war ich einkaufen und habe Brot und Milch geholt",
        "Gestern habe ich ferngesehen und dann geschlafen",
        "Das Wetter ist heute regnerisch und kalt",
        "Ich habe meine Pflanzen gegossen und den Rasen gemäht",
        "Heute Morgen habe ich Kaffee getrunken und Zeitung gelesen",
        "Der Müll muss morgen rausgebracht werden",
        "Ich habe die Wäsche gewaschen und aufgehängt",
        "Heute Abend koche ich Nudeln mit Tomatensoße",
        "Die Katze hat schon wieder auf dem Sofa geschlafen",
        "Ich muss noch die Rechnung für den Strom bezahlen",
        "Heute war ich beim Zahnarzt zur Kontrolle",
        "Der Briefkasten war mal wieder voller Werbung",
        "Ich habe den Tisch gedeckt und das Essen vorbereitet",
        "Die Nachbarn haben schon wieder laute Musik gehört",
        "Heute ist ein ruhiger Tag, ich mache gar nichts",
    ],

    "B_versteckt": [
        # Banale Sätze mit versteckten wichtigen Infos
        "Heute war ich einkaufen und habe zufällig gehört dass OpenAI GPT-5.2 einen IQ von 147 hat",
        "Beim Spazierengehen ist mir eingefallen dass Google Gemini 3 Flash viermal günstiger ist",
        "Beim Kaffeetrinken habe ich gelesen dass Salesforce Agentforce 360 die erste Plattform für Mensch-KI-Kollaboration ist",
        "Ich habe geträumt dass Microsoft ein Agentic OS entwickelt als Betriebssystem für KI-Agenten",
        "Beim Frühstück habe ich erfahren dass Nvidia Nemotron 3 Nano 30 Milliarden Parameter hat",
        "Gestern Abend habe ich im Newsfeed gesehen dass China ein KI-Supercenter-Netzwerk über 1243 Meilen baut",
        "Beim Zähneputzen ist mir eingefallen dass der Speed Act Genehmigungen von 6 Jahren auf 150 Tage verkürzt",
        "Auf dem Weg zur Arbeit habe ich gehört dass 57 Prozent der Unternehmen KI-Agenten im Einsatz haben",
        "Beim Mittagessen habe ich gelesen dass der KI-Agenten-Markt von 5 auf 50 Milliarden Dollar wächst",
        "Im Wartezimmer habe ich erfahren dass Open Source Modelle immer konkurrenzfähiger werden",
        "Beim Einschlafen ist mir eingefallen dass Xiaomi Mimo V2 mit 309 Milliarden Parametern released hat",
        "Während des Spülens habe ich gehört dass die EU KI-Fristen lockert",
        "Beim Joggen habe ich gesehen dass VW und Miele massiv Stellen abbauen",
        "In der U-Bahn habe ich gelesen dass Prompt-Injection eine wachsende Bedrohung ist",
        "Beim Einkaufen habe ich zufällig mitbekommen dass Human-in-the-Loop Modelle wichtiger werden",
    ],

    "C_informativ": [
        "GPT-5.2 erreicht 80 Prozent im SWE-bench für autonomes Programmieren und 100 Prozent in Mathematik AIME 2025",
        "Gemini 3 Flash kostet nur 50 Cent pro Million Input-Tokens und ist damit viermal günstiger als GPT-5.2",
        "Agentforce von Salesforce ermöglicht autonome KI-Agenten in beliebigen Geschäftsfunktionen rund um die Uhr",
        "Microsofts Copilot Studio ist eine Low-Code-Plattform zur Erstellung von KI-Agenten im Microsoft-Ökosystem",
        "Multi-Agenten-Systeme mit spezialisierten Agenten für Planung Programmierung Testen und Sicherheit werden Standard",
        "Nvidia Nemotron 3 Nano und Xiaomi Mimo V2 zeigen dass Open Source Modelle aufschließen",
        "Chinas KI-Supercenter-Netzwerk verbindet 40 Städte mit Glasfaser über 1243 Meilen",
        "Der Speed Act in den USA verkürzt Genehmigungsprozesse für KI-Rechenzentren drastisch",
        "Nur 21 Prozent der Unternehmen haben ausgereifte KI-Governance-Frameworks",
        "Coding-Agenten wie Claude Code Gemini CLI und OpenAI Codex arbeiten in stundenlangen autonomen Schleifen",
    ],

    "D_kurz": [
        "Termin 15:30 Zahnarzt",
        "Milch Brot Eier",
        "Meeting 14 Uhr",
        "Password: xK9#mP2",
        "Server neustarten",
        "Backup läuft",
        "Update installiert",
        "Logs prüfen",
        "API-Key: sk-abc123def456",
        "SSH: 192.168.1.100:22",
    ],
}


def main():
    gate = EntropyGate()
    print(f"Gate-Test-Injektor")
    print(f"  Threshold: {gate.config.threshold}")
    print(f"  Alpha: {gate.config.alpha}, Beta: {gate.config.beta}")
    print()

    total = sum(len(v) for v in TEXTE.values())
    stats = {"extract": 0, "ignore": 0, "skip": 0}
    results_by_cat = {}

    for cat, texte in TEXTE.items():
        cat_extract = 0
        cat_ignore = 0
        cat_skip = 0
        cat_name = {"A_banal": "A) Banale Alltagstexte",
                     "B_versteckt": "B) Versteckte Infos",
                     "C_informativ": "C) Normale Infotexte",
                     "D_kurz": "D) Kurze Notizen"}[cat]

        print(f"\n{'='*60}")
        print(f"  {cat_name} ({len(texte)} Einträge)")
        print(f"{'='*60}")

        for i, text in enumerate(texte, 1):
            try:
                event_id = gate.ingest(text, source="gate_test_injector", debug=False)
                if event_id:
                    # Nach dem ingest kurz prüfen was passiert ist
                    # (wir können nicht direkt auf gate_result zugreifen, aber wir sehen am event_id obs geklappt hat)
                    print(f"  [{i:2d}] ✅ {text[:55]:55s}", end="")
                    
                    # Kleine Verzögerung damit novelty sich aktualisiert
                    import time
                    time.sleep(0.1)
                else:
                    print(f"  [{i:2d}] ❌ {text[:55]:55s}")
            except Exception as e:
                print(f"  [{i:2d}] ❌ {text[:55]:55s} | {e}")

    print(f"\n{'='*60}")
    print(f"  Fertig! {total} Einträge erstellt.")
    print(f"  Jetzt memory_stats prüfen für gate_pass_rate")
    print(f"  Oder direkt in SurrealDB:")
    print(f"    SELECT decision, gate_score, text_score, novelty FROM gate_log ORDER BY gate_score ASC;")


if __name__ == "__main__":
    main()
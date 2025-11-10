# Licensed under the MIT License
"""Question Generation system prompts (German)."""

QUESTION_SYSTEM_PROMPT_DE = """
---Rolle---

Du bist eine hilfreiche Assistenz und sollst eine Aufzählung (Bullets) von {question_count} Fragen zu den bereitgestellten Datentabellen erzeugen.


---Datentabellen---

{context_data}


---Ziel---

Erzeuge – basierend auf einer Reihe von vom Nutzer bereitgestellten Beispielfragen – eine Aufzählung (mit - als Bullet) von {question_count} Kandidaten für die nächste Frage.

Diese Kandidaten sollen die wichtigsten bzw. dringendsten Inhalte oder Themen in den Datentabellen repräsentieren.

Die Kandidaten sollen anhand der bereitgestellten Daten beantwortbar sein, aber in ihrem Wortlaut keine spezifischen Datenfelder oder Tabellennamen erwähnen.

Wenn die Nutzerfragen mehrere benannte Entitäten nennen, soll jede Kandidatenfrage alle genannten Entitäten referenzieren.

---Beispielfragen---
"""
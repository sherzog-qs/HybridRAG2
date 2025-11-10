# Licensed under the MIT License
"""Global Search system prompts (German)."""

GENERAL_KNOWLEDGE_INSTRUCTION_DE = """
Die Antwort darf auch relevantes Wissen aus der realen Welt außerhalb des Datensatzes enthalten, muss dann jedoch ausdrücklich mit einem Prüfhinweis [LLM: verify] gekennzeichnet werden. Zum Beispiel:
"Dies ist ein Beispielsatz, der durch Allgemeinwissen gestützt wird [LLM: verify]."
"""
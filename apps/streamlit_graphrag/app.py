# -*- coding: utf-8 -*-
import os
import io
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure local package import when running from subdir
import sys
THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st
import pandas as pd
import requests
import time
import shutil

# Global terminal placeholder (initialized later in layout)
terminal_placeholder = None  # will hold st.empty() when created

# GraphRAG imports
from graphrag.cli.initialize import initialize_project_at
from graphrag.config.load_config import load_config
from graphrag.config.enums import IndexingMethod, SearchMethod
from graphrag.api.index import build_index
import graphrag.api.query as query_api
import graphrag.api.prompt_tune as prompt_api
from graphrag.utils.api import create_storage_from_config, reformat_context_data
from graphrag.utils.storage import load_table_from_storage, storage_has_table
# Prompt templates (for seeding without overwriting settings)
from graphrag.prompts.index.community_report import COMMUNITY_REPORT_PROMPT
from graphrag.prompts.index.community_report_text_units import COMMUNITY_REPORT_TEXT_PROMPT
from graphrag.prompts.index.extract_claims import EXTRACT_CLAIMS_PROMPT
from graphrag.prompts.index.extract_graph import GRAPH_EXTRACTION_PROMPT
from graphrag.prompts.index.summarize_descriptions import SUMMARIZE_PROMPT
from graphrag.prompts.query.basic_search_system_prompt import BASIC_SEARCH_SYSTEM_PROMPT
from graphrag.prompts.query.drift_search_system_prompt import (
    DRIFT_LOCAL_SYSTEM_PROMPT,
    DRIFT_REDUCE_PROMPT,
)
from graphrag.prompts.query.global_search_knowledge_system_prompt import (
    GENERAL_KNOWLEDGE_INSTRUCTION,
)
from graphrag.prompts.query.global_search_map_system_prompt import MAP_SYSTEM_PROMPT
from graphrag.prompts.query.global_search_reduce_system_prompt import (
    REDUCE_SYSTEM_PROMPT,
)
from graphrag.prompts.query.local_search_system_prompt import LOCAL_SEARCH_SYSTEM_PROMPT
from graphrag.prompts.query.question_gen_system_prompt import QUESTION_SYSTEM_PROMPT
# German variants for seeding
from graphrag.prompts.query.basic_search_system_prompt_de import BASIC_SEARCH_SYSTEM_PROMPT_DE
from graphrag.prompts.query.drift_search_system_prompt_de import (
    DRIFT_LOCAL_SYSTEM_PROMPT_DE,
    DRIFT_REDUCE_PROMPT_DE,
)
from graphrag.prompts.query.global_search_knowledge_system_prompt_de import (
    GENERAL_KNOWLEDGE_INSTRUCTION_DE,
)
from graphrag.prompts.query.global_search_map_system_prompt_de import MAP_SYSTEM_PROMPT_DE
from graphrag.prompts.query.global_search_reduce_system_prompt_de import (
    REDUCE_SYSTEM_PROMPT_DE,
)
from graphrag.prompts.query.local_search_system_prompt_de import LOCAL_SEARCH_SYSTEM_PROMPT_DE
from graphrag.prompts.query.question_gen_system_prompt_de import QUESTION_SYSTEM_PROMPT_DE

# --------------
# Helpers & Callbacks
# --------------

class StreamlitWorkflowCallbacks:
    """Streamlit UI callbacks for indexing progress (CLI-like Ladebalken + Terminal).

    Für extract_graph wird zusätzlich eine Pre-Progress-Anzeige (gestartet vs. abgeschlossen) angezeigt.
    """

    def __init__(
        self,
        container: "st.delta_generator.DeltaGenerator",
        write: Optional[Any] = None,
    ) -> None:
        self.container = container
        self._bars: dict[str, Any] = {}
        self._extra_bars: dict[str, dict[str, Any]] = {}
        self._counts: dict[str, dict[str, Any]] = {}
        self._current: str | None = None
        self._pipeline_area = None
        self._write = write

    def pipeline_start(self, names: list[str]) -> None:
        self._pipeline_area = self.container.container()
        msg = "Starte Pipeline: " + ", ".join(names)
        self._pipeline_area.write(msg)
        if self._write:
            self._write(msg)

    def pipeline_end(self, results: list[Any]) -> None:
        if self._pipeline_area:
            self._pipeline_area.success("Pipeline abgeschlossen")
        if self._write:
            self._write("Pipeline abgeschlossen")

    def workflow_start(self, name: str, instance: object) -> None:
        self._current = name
        sect = self.container.expander(f"Workflow: {name}", expanded=True)
        bar = sect.progress(0, text=f"{name}: 0%")
        self._bars[name] = (sect, bar)
        # Zähler initialisieren
        self._counts[name] = {"started": 0, "completed": 0, "total": None, "t0": time.time()}
        # Zusatzbalken + Status für extract_graph
        if name == "extract_graph":
            subcol1, subcol2 = sect.columns(2)
            status = sect.empty()
            self._extra_bars[name] = {
                "started": subcol1.progress(0, text="Gestartet: 0%"),
                "completed": subcol2.progress(0, text="Abgeschlossen: 0%"),
                "status": status,
            }
        if self._write:
            self._write(f"Starting workflow: {name}")

    def workflow_end(self, name: str, instance: object) -> None:
        sect_bar = self._bars.get(name)
        if sect_bar:
            sect, bar = sect_bar
            bar.progress(100, text=f"{name}: 100%")
            sect.success(f"Workflow abgeschlossen: {name}")
        if self._write:
            self._write(f"Workflow complete: {name}")

    def progress(self, progress) -> None:
        try:
            name = self._current
            if not name:
                return
            sect_bar = self._bars.get(name)
            if not sect_bar:
                return
            sect, bar = sect_bar
            total = progress.total_items or 1
            done = progress.completed_items or 0
            pct = int((done / total) * 100) if total else 0

            desc = (progress.description or "").lower()
            extra = self._extra_bars.get(name)

            if name == "extract_graph" and extra:
                # Zähler aktualisieren
                cnt = self._counts.get(name, {})
                if "started" in desc:
                    cnt["started"], cnt["total"] = done, total
                    started_pct = int((done / total) * 100)
                    extra["started"].progress(max(min(started_pct, 100), 0), text=f"Gestartet: {done}/{total} ({started_pct}%)")
                    # Hauptbalken NICHT mit Started-Events updaten
                elif "extract graph progress" in desc:
                    cnt["completed"], cnt["total"] = done, total
                    completed_pct = int((done / total) * 100)
                    extra["completed"].progress(max(min(completed_pct, 100), 0), text=f"Abgeschlossen: {done}/{total} ({completed_pct}%)")
                    # Hauptbalken nur auf Completed-Events setzen
                    bar.progress(max(min(completed_pct, 100), 0), text=f"{name}: {done}/{total} ({completed_pct}%)")
                # Statuszeile: queued/running/done + ETA
                s = cnt.get("started", 0)
                c = cnt.get("completed", 0)
                t = cnt.get("total", total) or total
                queued = max(t - s, 0)
                running = max(s - c, 0)
                done_c = c
                elapsed = max(time.time() - cnt.get("t0", time.time()), 1e-6)
                rate = c / elapsed
                eta = (t - c) / rate if rate > 0 else None
                eta_txt = f", ETA ~{int(eta)}s" if eta else ""
                extra["status"].markdown(f"Queued: {queued} · Running: {running} · Done: {done_c}/{t} · Rate: {rate:.2f}/s{eta_txt}")
            else:
                # Default: Hauptbalken normal updaten
                bar.progress(max(min(pct, 100), 0), text=f"{name}: {done}/{total} ({pct}%)")

            if self._write and (pct % 5 == 0):  # log alle 5%
                self._write(f"{name}: {done}/{total} ({pct}%)")
        except Exception:
            # UI errors should never break indexing
            pass

def germanize_prompt_file(prompt_path: Path) -> None:
    try:
        if not prompt_path.exists():
            return
        text = prompt_path.read_text(encoding="utf-8")
        directive = (
            "\n\n[Sprache]\nBitte alle Ausgaben ausschließlich in deutscher Sprache formulieren.\n"
        )
        if "deutscher Sprache" not in text:
            prompt_path.write_text(text.strip() + directive, encoding="utf-8")
    except Exception:
        pass


def apply_german_defaults(root_dir: Path) -> None:
    prompts_dir = root_dir / "prompts"
    if prompts_dir.is_dir():
        for fname in [
            "local_search_system_prompt.txt",
            "global_search_map_system_prompt.txt",
            "global_search_reduce_system_prompt.txt",
            "global_search_knowledge_system_prompt.txt",
            "drift_search_system_prompt.txt",
            "drift_search_reduce_prompt.txt",
            "basic_search_system_prompt.txt",
        ]:
            germanize_prompt_file(prompts_dir / fname)


def _translate_en_to_de_prompt(text: str) -> str:
    """Very simple rule-based translation for our fixed prompt patterns.
    Keeps JSON keys/structure intact and focuses on headings and instruction sentences.
    """
    replacements = [
        ("---Role---", "---Rolle---"),
        ("---Goal---", "---Ziel---"),
        ("---Data tables---", "---Datentabellen---"),
        ("---Target response length and format---", "---Zielantwortlänge und -format---"),
        ("You are a helpful assistant", "Du bist eine hilfreiche Assistenz"),
        ("Generate a response", "Erzeuge eine Antwort"),
        ("that responds to the user's question", "die die Frage des Nutzers beantwortet"),
        ("summarizing all information", "und fasst alle Informationen zusammen"),
        ("If you don't know the answer, just say so. Do not make anything up.", "Wenn du die Antwort nicht weißt, sage dies explizit. Erfinde nichts."),
        ("Points supported by data should list their data references as follows:", "Durch Daten gestützte Aussagen sollen ihre Datenquellen wie folgt angeben:"),
        ("Do not list more than 5 record ids", "Führe nicht mehr als 5 Datensatz‑IDs in einem Verweis auf"),
        ("Instead, list the top 5 most relevant record ids", "Gib stattdessen die 5 relevantesten IDs an"),
        ("For example:", "Zum Beispiel:"),
        ("where", "wobei"),
        ("represent the id (not the index) of the relevant data record", "die ID (nicht der Index) des relevanten Datensatzes darstellen"),
        ("Do not include information where the supporting evidence for it is not provided.", "Führe keine Informationen an, für die keine Belege vorhanden sind."),
        ("Add sections and commentary to the response as appropriate for the length and format. Style the response in markdown.", "Füge je nach Länge und Format geeignete Abschnitte und Kommentare hinzu. Formatiere die Antwort in Markdown."),
        ("The response should be JSON formatted as follows:", "Formatiere die Antwort wie folgt in JSON:"),
        ("The response shall preserve the original meaning", "Die Antwort muss die ursprüngliche Bedeutung"),
        ("Limit your response length to", "Begrenze die Antwortlänge auf"),
        ("The response may also include relevant real-world knowledge", "Die Antwort darf auch relevante allgemeine Kenntnisse enthalten"),
        ("but it must be explicitly annotated with a verification tag", "muss dann jedoch ausdrücklich mit einem Prüfhinweis gekennzeichnet werden"),
    ]
    out = text
    for a, b in replacements:
        out = out.replace(a, b)
    # Prepend German language banner
    banner = (
        "[Sprache – Deutsch]\n"
        "Bitte alle Antworten, Überlegungen und Erklärungen ausschließlich in deutscher Sprache formulieren.\n"
        "JSON‑Schlüssel/Bezeichner müssen exakt beibehalten werden.\n\n"
    )
    return banner + out


def ensure_de_query_prompts(root_dir: Path) -> int:
    """Create side-by-side German versions (_de.txt) of all query prompts.

    Leaves English originals untouched. Returns number of new DE files created.
    """
    prompts_dir = root_dir / "prompts"
    if not prompts_dir.is_dir():
        return 0
    prompt_names = [
        "drift_search_system_prompt.txt",
        "drift_search_reduce_prompt.txt",
        "global_search_map_system_prompt.txt",
        "global_search_reduce_system_prompt.txt",
        "global_search_knowledge_system_prompt.txt",
        "local_search_system_prompt.txt",
        "basic_search_system_prompt.txt",
        "question_gen_system_prompt.txt",
    ]
    created = 0
    for name in prompt_names:
        src = prompts_dir / name
        if not src.exists():
            continue
        dst = prompts_dir / (src.stem + "_de" + src.suffix)
        if dst.exists():
            continue
        try:
            text = src.read_text(encoding="utf-8")
            de = _translate_en_to_de_prompt(text)
            dst.write_text(de, encoding="utf-8")
            created += 1
        except Exception:
            pass
    return created


def prefer_de_query_prompts(cfg, root_dir: Path) -> None:
    """Override config prompt paths to DE variants if present."""
    def de(path: str | None) -> str | None:
        if not path:
            return path
        p = Path(root_dir) / path
        cand = p.with_stem(p.stem + "_de")
        return str(cand.relative_to(root_dir)) if cand.exists() else path

    # local search
    cfg.local_search.prompt = de(cfg.local_search.prompt)
    # global
    cfg.global_search.map_prompt = de(cfg.global_search.map_prompt)
    cfg.global_search.reduce_prompt = de(cfg.global_search.reduce_prompt)
    cfg.global_search.knowledge_prompt = de(cfg.global_search.knowledge_prompt)
    # drift
    cfg.drift_search.prompt = de(cfg.drift_search.prompt)
    cfg.drift_search.reduce_prompt = de(cfg.drift_search.reduce_prompt)
    # basic
    cfg.basic_search.prompt = de(cfg.basic_search.prompt)


def ensure_de_index_prompts(root_dir: Path) -> int:
    """Create side-by-side German versions (_de.txt) of all indexing prompts."""
    prompts_dir = root_dir / "prompts"
    if not prompts_dir.is_dir():
        return 0
    prompt_names = [
        "extract_graph.txt",
        "summarize_descriptions.txt",
        "extract_claims.txt",
        "community_report_graph.txt",
        "community_report_text.txt",
    ]
    created = 0
    for name in prompt_names:
        src = prompts_dir / name
        if not src.exists():
            continue
        dst = prompts_dir / (src.stem + "_de" + src.suffix)
        if dst.exists():
            continue
        try:
            text = src.read_text(encoding="utf-8")
            de = _translate_en_to_de_prompt(text)
            dst.write_text(de, encoding="utf-8")
            created += 1
        except Exception:
            pass
    return created


def prefer_de_index_prompts(cfg, root_dir: Path) -> None:
    """Override indexing prompt paths to DE variants if present."""
    def de(path: str | None) -> str | None:
        if not path:
            return path
        p = Path(root_dir) / path
        cand = p.with_stem(p.stem + "_de")
        return str(cand.relative_to(root_dir)) if cand.exists() else path

    cfg.extract_graph.prompt = de(cfg.extract_graph.prompt)
    cfg.summarize_descriptions.prompt = de(cfg.summarize_descriptions.prompt)
    if cfg.extract_claims and hasattr(cfg.extract_claims, "prompt"):
        cfg.extract_claims.prompt = de(cfg.extract_claims.prompt)
    cfg.community_reports.graph_prompt = de(cfg.community_reports.graph_prompt)
    cfg.community_reports.text_prompt = de(cfg.community_reports.text_prompt)


def ensure_root_initialized(root_dir: Path, force: bool) -> None:
    initialize_project_at(root_dir, force)
    apply_german_defaults(root_dir)


def purge_project_outputs(root_dir: Path, cfg, clear_inputs: bool = False) -> list[str]:
    """Delete output/cache/reporting/update outputs and local vector store under the given root.
    Returns a list of removed paths (relative to root).
    """
    removed: list[str] = []
    def _rm_path(p: Path):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
            if p.exists():
                # If still exists (e.g., permission), skip recording
                return
            removed.append(str(p.relative_to(root_dir)))
        except Exception:
            pass

    try:
        # Output dirs
        out_dir = (root_dir / Path(cfg.output.base_dir)).resolve()
        cache_dir = (root_dir / Path(cfg.cache.base_dir)).resolve()
        rep_dir = (root_dir / Path(cfg.reporting.base_dir)).resolve()
        upd_dir = (root_dir / Path(cfg.update_index_output.base_dir)).resolve() if getattr(cfg, 'update_index_output', None) else None
        # Vector store (only local lancedb)
        for vs in (cfg.vector_store or {}).values():
            db_uri = getattr(vs, 'db_uri', None)
            vs_type = getattr(vs, 'type', None)
            if vs_type == 'lancedb' and db_uri:
                vs_path = Path(db_uri)
                if not vs_path.is_absolute():
                    vs_path = (root_dir / vs_path).resolve()
                _rm_path(vs_path)
        for p in [out_dir, cache_dir, rep_dir, upd_dir]:
            if p:
                _rm_path(p)
        # Query log in root
        _rm_path(root_dir / 'query.log')
        # Optional: clear inputs
        if clear_inputs:
            inp_dir = (root_dir / Path(cfg.input.storage.base_dir)).resolve()
            if inp_dir.exists():
                for child in inp_dir.glob('*'):
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                removed.append(str(inp_dir.relative_to(root_dir)) + '/*')
    except Exception:
        pass
    return removed


def _get_chat_llm_config(cfg):
    try:
        chat_id = cfg.local_search.chat_model_id or cfg.global_search.chat_model_id
        return cfg.get_language_model_config(chat_id)
    except Exception:
        return None


def _get_embed_config(cfg):
    try:
        emb_id = cfg.embed_text.model_id
        return cfg.get_language_model_config(emb_id)
    except Exception:
        return None


def test_embedding_connection(cfg) -> tuple[bool, str]:
    """Test the embeddings endpoint via OpenAI-compatible /v1/embeddings."""
    emb = _get_embed_config(cfg)
    if not emb:
        return False, "Embedding-Konfiguration nicht gefunden"
    base = getattr(emb, "api_base", None) or "http://127.0.0.1:8000"
    # Ask server for its configured model
    try:
        h = requests.get(base.rstrip("/") + "/", timeout=5)
        server_model = h.json().get("model") if h.status_code == 200 else None
    except Exception:
        server_model = None
    url = base.rstrip("/") + "/v1/embeddings"
    payload = {"input": "Hallo Welt", "model": server_model or getattr(emb, "model", None) or getattr(emb, "deployment_name", None)}
    headers = {"Content-Type": "application/json"}
    # optional auth if vorhanden
    if getattr(emb, "api_key", None) and "127.0.0.1" not in base:
        headers["Authorization"] = f"Bearer {emb.api_key}"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 200 and isinstance(r.json().get("data", []), list):
            dim = len(r.json()["data"][0].get("embedding", [])) if r.json()["data"] else 0
            return True, f"OK (dim={dim})"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def test_llm_connection(cfg) -> tuple[bool, str]:
    """Test the chat completion via OpenAI /v1/chat/completions using configured model."""
    chat = _get_chat_llm_config(cfg)
    if not chat:
        return False, "Chat-LLM-Konfiguration nicht gefunden"
    base = getattr(chat, "api_base", None) or "https://api.openai.com"
    model = getattr(chat, "model", None) or getattr(chat, "deployment_name", None) or "gpt-4.1-mini"
    key = getattr(chat, "api_key", None) or os.environ.get("GRAPHRAG_API_KEY")
    if not key:
        return False, "Kein API Key gefunden (GRAPHRAG_API_KEY)"
    url = base.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Antworte mit OK."},
            {"role": "user", "content": "Sag OK"},
        ],
        "max_tokens": 4,
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code == 200:
            txt = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            return True, f"OK ({txt.strip()[:50]})"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def save_uploaded_files(files: List[Any], dest_dir: Path) -> List[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []
    for f in files:
        if f is None:
            continue
        # Streamlit returns UploadedFile-like objects
        data = f.getvalue() if hasattr(f, "getvalue") else f.read()
        if data is None:
            continue
        out_path = dest_dir / f.name
        with out_path.open("wb") as out:
            out.write(data)
        saved.append(out_path)
    return saved


async def _load_outputs_single_index(config) -> Dict[str, pd.DataFrame]:
    storage = create_storage_from_config(config.output)
    required = [
        "entities",
        "communities",
        "community_reports",
        "text_units",
        "relationships",
    ]
    optional = ["covariates"]

    dfs: Dict[str, pd.DataFrame] = {}
    for name in required:
        dfs[name] = await load_table_from_storage(name=name, storage=storage)
    for name in optional:
        if await storage_has_table(name, storage):
            dfs[name] = await load_table_from_storage(name=name, storage=storage)
        else:
            dfs[name] = None  # type: ignore
    return dfs


async def _load_outputs_basic_single(config) -> Dict[str, pd.DataFrame]:
    storage = create_storage_from_config(config.output)
    return {"text_units": await load_table_from_storage("text_units", storage)}


def run_async(coro):
    return asyncio.run(coro)


# --------------
# UI
# --------------
st.set_page_config(page_title="GraphRAG GUI", layout="wide")
st.title("GraphRAG – Streamlit GUI (Deutsch)")

# Helper: seed prompts without touching settings.yaml/.env when external config is used

def seed_prompts_only(root_dir: Path, force: bool = False) -> int:
    prompts_dir = root_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    # English originals
    prompts_en = {
        "extract_graph": GRAPH_EXTRACTION_PROMPT,
        "summarize_descriptions": SUMMARIZE_PROMPT,
        "extract_claims": EXTRACT_CLAIMS_PROMPT,
        "community_report_graph": COMMUNITY_REPORT_PROMPT,
        "community_report_text": COMMUNITY_REPORT_TEXT_PROMPT,
        "drift_search_system_prompt": DRIFT_LOCAL_SYSTEM_PROMPT,
        "drift_reduce_prompt": DRIFT_REDUCE_PROMPT,
        "global_search_map_system_prompt": MAP_SYSTEM_PROMPT,
        "global_search_reduce_system_prompt": REDUCE_SYSTEM_PROMPT,
        "global_search_knowledge_system_prompt": GENERAL_KNOWLEDGE_INSTRUCTION,
        "local_search_system_prompt": LOCAL_SEARCH_SYSTEM_PROMPT,
        "basic_search_system_prompt": BASIC_SEARCH_SYSTEM_PROMPT,
        "question_gen_system_prompt": QUESTION_SYSTEM_PROMPT,
    }
    # German counterparts
    prompts_de = {
        "drift_search_system_prompt_de": DRIFT_LOCAL_SYSTEM_PROMPT_DE,
        "drift_reduce_prompt_de": DRIFT_REDUCE_PROMPT_DE,
        "global_search_map_system_prompt_de": MAP_SYSTEM_PROMPT_DE,
        "global_search_reduce_system_prompt_de": REDUCE_SYSTEM_PROMPT_DE,
        "global_search_knowledge_system_prompt_de": GENERAL_KNOWLEDGE_INSTRUCTION_DE,
        "local_search_system_prompt_de": LOCAL_SEARCH_SYSTEM_PROMPT_DE,
        "basic_search_system_prompt_de": BASIC_SEARCH_SYSTEM_PROMPT_DE,
        "question_gen_system_prompt_de": QUESTION_SYSTEM_PROMPT_DE,
    }
    created = 0
    for name, content in prompts_en.items():
        prompt_file = prompts_dir / f"{name}.txt"
        if not prompt_file.exists() or force:
            prompt_file.write_text(content, encoding="utf-8")
            created += 1
    for name, content in prompts_de.items():
        prompt_file = prompts_dir / f"{name}.txt"
        if not prompt_file.exists() or force:
            prompt_file.write_text(content, encoding="utf-8")
            created += 1
    return created

with st.sidebar:
    st.header("Projekt")
    default_root_env = os.environ.get("GRAPHRAG_DEFAULT_ROOT")
    default_root = (
        str(Path(default_root_env).resolve())
        if default_root_env
        else str((PROJECT_ROOT / "christmas").resolve())
    )
    root_str = st.text_input("Root-Verzeichnis", value=default_root)
    root_dir = Path(root_str)

    # Optional: Externe Konfiguration im Git-Root (oder beliebig)
    default_cfg = str((PROJECT_ROOT / "settings.yaml").resolve())
    cfg_str = st.text_input(
        "Config-Datei (optional)", value=default_cfg,
        help="Pfad zu settings.yaml. Liegt die Datei hier, wird die .env daneben geladen. Leer lassen, um <root>/settings.yaml zu nutzen."
    )
    cfg_path: Path | None = None
    if cfg_str.strip():
        p = Path(cfg_str.strip())
        cfg_path = p if p.exists() else None
        if cfg_path is None:
            st.warning("Angegebene Config-Datei nicht gefunden – es wird die Konfig im Root-Verzeichnis verwendet.")

    clear_inputs = st.checkbox("Input-Ordner beim Neuinitialisieren leeren", value=False)

    colA, colB = st.columns(2)
    with colA:
        if st.button("Initialisieren", help="graphrag init --root <pfad>"):
            try:
                if cfg_path is not None:
                    st.info("Externe Config aktiv – generiere nur Prompt-Dateien im Projekt-Root.")
                    n = seed_prompts_only(root_dir, force=False)
                    apply_german_defaults(root_dir)
                    st.success(f"Prompts vorbereitet ({n} Datei(en))")
                else:
                    ensure_root_initialized(root_dir, force=False)
                st.success(f"Projekt initialisiert: {root_dir}")
            except ValueError:
                st.info("Bereits initialisiert. Nutze 'Neu initialisieren' zum Überschreiben.")
    with colB:
        if st.button("Neu initialisieren", help="Outputs/Cache/VectorStore löschen und ggf. Projekt neu anlegen"):
            try:
                cfg_for_purge = load_config(root_dir, cfg_path) if cfg_path else load_config(root_dir)
            except Exception:
                cfg_for_purge = None
            if cfg_for_purge is not None:
                removed = purge_project_outputs(root_dir, cfg_for_purge, clear_inputs=clear_inputs)
                st.success(f"Bereinigt: {', '.join(removed) if removed else 'nichts zu entfernen'}")
            else:
                st.warning("Konfiguration konnte nicht geladen werden – Überspringe Bereinigung.")
            if cfg_path is not None:
                st.info("Externe Config aktiv – settings.yaml/.env im Projekt-Root werden nicht überschrieben. Prompts werden neu vorbereitet.")
                n = seed_prompts_only(root_dir, force=True)
                apply_german_defaults(root_dir)
                st.success(f"Prompts vorbereitet ({n} Datei(en))")
            else:
                ensure_root_initialized(root_dir, force=True)
                st.success(f"Projekt neu initialisiert: {root_dir}")

    st.toggle("Deutsch als Standardsprache", value=True, key="german_default")
    if st.session_state.get("german_default"):
        # Deutsch aktiv: bestehende Prompts mit deutscher Ausgaberichtlinie versehen (kein _de‑Umschalten)
        apply_german_defaults(root_dir)

    st.divider()

    st.caption("Verbindungen testen")
    colt1, colt2 = st.columns(2)
    with colt1:
        if st.button("Embedding-Server testen"):
            try:
                cfg = load_config(root_dir, cfg_path) if cfg_path else load_config(root_dir)
                ok, msg = test_embedding_connection(cfg)
                (st.success if ok else st.error)(msg)
            except Exception as e:
                st.error(f"Fehler beim Test: {e}")
    with colt2:
        if st.button("LLM-Verbindung testen"):
            try:
                cfg = load_config(root_dir, cfg_path) if cfg_path else load_config(root_dir)
                ok, msg = test_llm_connection(cfg)
                (st.success if ok else st.error)(msg)
            except Exception as e:
                st.error(f"Fehler beim Test: {e}")

    st.divider()
    st.caption("Index erstellen")
    idx_method = st.selectbox(
        "Indexierungsmethode",
        [IndexingMethod.Standard.value, IndexingMethod.Fast.value],
        index=0,
    )
    if st.button("Index bauen"):
        try:
            cfg = load_config(root_dir, cfg_path) if cfg_path else load_config(root_dir)
            if st.session_state.get("german_default"):
                # Nur deutsche Ausgaberichtlinie an bestehende Prompts anhängen (keine _de‑Dateien verwenden)
                apply_german_defaults(root_dir)
            st.write("Starte Indexierung… Dies kann je nach Daten dauern.")

            # Terminal writer
            if "terminal_lines" not in st.session_state:
                st.session_state["terminal_lines"] = []
            def _write(msg: str):
                st.session_state["terminal_lines"].append(msg)
                if len(st.session_state["terminal_lines"]) > 500:
                    st.session_state["terminal_lines"] = st.session_state["terminal_lines"][-500:]
                if terminal_placeholder is not None:
                    terminal_placeholder.code("\n".join(st.session_state["terminal_lines"]), language="bash")

            callbacks = [StreamlitWorkflowCallbacks(st.container(), write=_write)]
            run_async(
                build_index(
                    config=cfg,
                    method=idx_method,
                    is_update_run=False,
                    verbose=True,
                    callbacks=callbacks,
                )
            )
            st.success("Indexierung abgeschlossen.")
        except Exception as e:
            st.error(f"Fehler bei der Indexierung: {e}")


tab_upload, tab_chat, tab_tune, tab_debug = st.tabs(["Dokumente hochladen", "Chat", "Prompt Tuning", "Debug"]) 

# --------------
# Upload
# --------------
with tab_upload:
    st.subheader("Dokumente in den Graphen laden")
    st.markdown("Wähle Dateien aus und lade sie in den Eingabeordner deines Projekts.")

    try:
        cfg = load_config(root_dir, cfg_path) if cfg_path else load_config(root_dir)
        input_base = Path(cfg.input.storage.base_dir)
        input_dir = root_dir / input_base
    except Exception:
        input_dir = root_dir / "input"

    files = st.file_uploader(
        "Dateien auswählen", accept_multiple_files=True, type=None, key="upload_files"
    )
    # Ensure persistence across reruns
    selected_files = st.session_state.get("upload_files", files)
    if st.button("Dateien hochladen"):
        if selected_files:
            saved = save_uploaded_files(selected_files, input_dir)
            st.success(f"{len(saved)} Datei(en) gespeichert in: {input_dir}")
            for p in saved:
                st.write(str(p.relative_to(root_dir)))
        else:
            st.warning("Keine Dateien ausgewählt.")

# --------------
# Chat
# --------------
with tab_chat:
    st.subheader("Mit GraphRAG chatten")
    st.markdown(
        "Wähle eine Suchmethode und stelle eine Frage. Standardmäßig wird auf Deutsch geantwortet."
    )

    method = st.radio(
        "Suchmethode",
        [
            SearchMethod.GLOBAL.value,
            SearchMethod.LOCAL.value,
            SearchMethod.DRIFT.value,
            SearchMethod.BASIC.value,
        ],
        index=0,
        horizontal=True,
    )

    query_text = st.text_input("Frage", value="Was sind die wichtigsten Themen dieser Geschichte?")

    col1, col2, col3 = st.columns(3)
    with col1:
        community_level = st.number_input("Community-Level", min_value=0, value=2)
    with col2:
        dyn_comm = st.checkbox("Dynamische Community-Auswahl (Global)", value=False)
    with col3:
        response_type = st.selectbox("Antwortstil", ["mehrere Absätze", "stichpunkte", "kurz"], index=0)

    if st.button("Absenden") and query_text:
        try:
            cfg = load_config(root_dir, cfg_path) if cfg_path else load_config(root_dir)
            if st.session_state.get("german_default"):
                # Nur deutsche Ausgaberichtlinie an bestehende Prompts anhängen (keine _de‑Dateien verwenden)
                apply_german_defaults(root_dir)

            if method == SearchMethod.BASIC.value:
                dfs = run_async(_load_outputs_basic_single(cfg))
                text_units = dfs["text_units"]
                answer, context = run_async(
                    query_api.basic_search(
                        config=cfg,
                        text_units=text_units,
                        query=query_text,
                        verbose=False,
                    )
                )
            elif method == SearchMethod.LOCAL.value:
                dfs = run_async(_load_outputs_single_index(cfg))
                answer, context = run_async(
                    query_api.local_search(
                        config=cfg,
                        entities=dfs["entities"],
                        communities=dfs["communities"],
                        community_reports=dfs["community_reports"],
                        text_units=dfs["text_units"],
                        relationships=dfs["relationships"],
                        covariates=dfs.get("covariates"),
                        community_level=int(community_level),
                        response_type=str(response_type),
                        query=query_text,
                        verbose=False,
                    )
                )
            elif method == SearchMethod.DRIFT.value:
                dfs = run_async(_load_outputs_single_index(cfg))
                answer, context = run_async(
                    query_api.drift_search(
                        config=cfg,
                        entities=dfs["entities"],
                        communities=dfs["communities"],
                        community_reports=dfs["community_reports"],
                        text_units=dfs["text_units"],
                        relationships=dfs["relationships"],
                        community_level=int(community_level),
                        response_type=str(response_type),
                        query=query_text,
                        verbose=False,
                    )
                )
            else:  # GLOBAL
                dfs = run_async(_load_outputs_single_index(cfg))
                answer, context = run_async(
                    query_api.global_search(
                        config=cfg,
                        entities=dfs["entities"],
                        communities=dfs["communities"],
                        community_reports=dfs["community_reports"],
                        community_level=int(community_level),
                        dynamic_community_selection=bool(dyn_comm),
                        response_type=str(response_type),
                        query=query_text,
                        verbose=False,
                    )
                )

            st.markdown("### Antwort")
            st.write(answer)

            st.markdown("### Kontext")
            try:
                reformatted = reformat_context_data(context)
                with st.expander("Kontextdaten anzeigen"):
                    for key, records in reformatted.items():
                        if isinstance(records, list) and len(records) > 0 and isinstance(records[0], dict):
                            st.markdown(f"**{key}**")
                            st.dataframe(pd.DataFrame(records))
            except Exception:
                pass

        except Exception as e:
            st.error(f"Fehler bei der Abfrage: {e}")

# --------------
# Prompt Tuning
# --------------
with tab_tune:
    st.subheader("Prompt Tuning (Indexing)")
    st.caption("Erstellt abgestimmte Prompts aus deinen Dokumenten und zeigt sie an.")

    colA, colB, colC = st.columns(3)
    with colA:
        limit = st.number_input("Anzahl Text-Chunks", min_value=1, max_value=1000, value=15)
    with colB:
        chunk_size = st.number_input("Chunk-Größe (Tokens)", min_value=64, max_value=8192, value=1024, step=64)
    with colC:
        overlap = st.number_input("Overlap (Tokens)", min_value=0, max_value=2048, value=128, step=32)

    selection = st.selectbox("Auswahlmethode", ["RANDOM", "AUTO"], index=0)
    language = st.text_input("Zielsprache", value="Deutsch")
    discover_types = st.checkbox("Entitätstypen automatisch erkennen", value=True)

if st.button("Prompts generieren"):
        try:
            cfg = load_config(root_dir, cfg_path) if 'cfg_path' in locals() and cfg_path else load_config(root_dir)
            sel_type = prompt_api.DocSelectionType.AUTO if selection == "AUTO" else prompt_api.DocSelectionType.RANDOM
            p1, p2, p3 = run_async(
                prompt_api.generate_indexing_prompts(
                    config=cfg,
                    chunk_size=int(chunk_size),
                    overlap=int(overlap),
                    limit=int(limit),
                    selection_method=sel_type,
                    language=language,
                    discover_entity_types=bool(discover_types),
                    verbose=False,
                )
            )
            st.success("Prompts generiert.")
            st.markdown("#### Extraktions-Prompt")
            st.code(p1, language="markdown")
            st.markdown("#### Entitäts-Summary-Prompt")
            st.code(p2, language="markdown")
            st.markdown("#### Community-Summary-Prompt")
            st.code(p3, language="markdown")
        except Exception as e:
            st.error(f"Fehler beim Prompt Tuning: {e}")

# --------------
# Terminal / Live-Logs unten
# --------------
st.divider()
st.subheader("Terminal / Live-Logs")

# Terminal state
if "terminal_lines" not in st.session_state:
    st.session_state["terminal_lines"] = []

btn_col1, btn_col2, btn_col3 = st.columns(3)
with btn_col1:
    if st.button("Leeren"):
        st.session_state["terminal_lines"] = []
with btn_col2:
    if st.button("Logs aktualisieren"):
        # Tail embedding server log
        try:
            log_path = Path("logs/embedding_server.log")
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
                st.session_state["terminal_lines"].extend([f"[embedding] {l}" for l in lines])
        except Exception:
            pass
        # Optional: weitere Logs (z. B. query.log)
        try:
            cfg = load_config(root_dir)
            query_log = Path(root_dir) / "query.log"
            if query_log.exists():
                qlines = query_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
                st.session_state["terminal_lines"].extend([f"[query] {l}" for l in qlines])
        except Exception:
            pass
with btn_col3:
    auto_scroll = st.checkbox("Auto-Scroll", value=True)

terminal_placeholder = st.empty()
terminal_placeholder.code("\n".join(st.session_state["terminal_lines"]), language="bash")

if auto_scroll:
    # Kleiner Trick: forciere Neurendering
    time.sleep(0.02)

# --------------
# Debug
# --------------
with tab_debug:
    st.subheader("Debug – Pipeline & Daten prüfen")

    # Effektive Konfiguration anzeigen
    try:
        cfg = load_config(root_dir, cfg_path) if 'cfg_path' in locals() and cfg_path else load_config(root_dir)
        colA, colB, colC = st.columns(3)
        with colA:
            st.markdown("**Root**")
            st.code(str(root_dir))
            st.markdown("**Input base_dir**")
            st.code(str(cfg.input.storage.base_dir))
            st.markdown("**Input file_type**")
            st.code(str(getattr(cfg.input, 'file_type', 'text')))
        with colB:
            st.markdown("**Output base_dir**")
            st.code(str(cfg.output.base_dir))
            st.markdown("**VectorStore**")
            vs = next(iter(cfg.vector_store.values())) if cfg.vector_store else None
            st.code(str(vs.model_dump() if vs else {}))
        with colC:
            inp_dir = (root_dir / Path(cfg.input.storage.base_dir)).resolve()
            out_dir = (root_dir / Path(cfg.output.base_dir)).resolve()
            st.markdown("**Effektive Pfade**")
            st.code(f"input_dir: {inp_dir}\noutput_dir: {out_dir}")
    except Exception as e:
        st.error(f"Konfiguration konnte nicht geladen werden: {e}")
        cfg = None

    st.markdown("---")
    st.markdown("### Eingaben im Input-Ordner")
    if cfg:
        try:
            inp_dir = (root_dir / Path(cfg.input.storage.base_dir)).resolve()
            files = []
            if inp_dir.exists():
                for p in sorted(inp_dir.glob("**/*")):
                    if p.is_file():
                        files.append(str(p.relative_to(root_dir)))
            st.write(f"{len(files)} Datei(en) gefunden")
            st.code("\n".join(files[:200]) or "<leer>")
        except Exception:
            st.warning("Input-Verzeichnis konnte nicht gelistet werden.")

    st.markdown("---")
    st.markdown("### Outputs prüfen (Parquet)")
    if cfg:
        out_dir = (root_dir / Path(cfg.output.base_dir)).resolve()
        def _head_parquet(name: str, cols: list[str] | None = None, limit: int = 10):
            try:
                import pandas as pd  # lazy
                path = out_dir / f"{name}.parquet"
                if not path.exists():
                    st.info(f"{name}.parquet nicht gefunden")
                    return
                df = pd.read_parquet(path)
                st.write(f"{name}: {len(df)} Zeilen")
                if cols:
                    show = [c for c in cols if c in df.columns]
                    st.dataframe(df[show].head(limit))
                else:
                    st.dataframe(df.head(limit))
            except Exception as ex:
                st.error(f"Fehler beim Lesen von {name}.parquet: {ex}")
        colsA, colsB = st.columns(2)
        with colsA:
            _head_parquet("documents", ["id","title"]) 
            _head_parquet("text_units", ["id","human_readable_id","text"]) 
            _head_parquet("entities", ["title","type","frequency"]) 
        with colsB:
            _head_parquet("relationships", ["source","target","weight"]) 
            _head_parquet("community_reports", ["community","title"]) 

    st.markdown("---")
    st.markdown("### Keyword-Test in Dokumenten/Text-Units")
    kw = st.text_input("Suchbegriff (Regex)", value=r"bcbs\s*239|bcbs|239")
    if st.button("Suchen") and cfg:
        try:
            import re, pandas as pd
            out_dir = (root_dir / Path(cfg.output.base_dir)).resolve()
            docs = pd.read_parquet(out_dir/"documents.parquet") if (out_dir/"documents.parquet").exists() else pd.DataFrame()
            tus  = pd.read_parquet(out_dir/"text_units.parquet") if (out_dir/"text_units.parquet").exists() else pd.DataFrame()
            rx = re.compile(kw, flags=re.I)
            st.write("Dokumente mit Treffer im Titel:")
            if not docs.empty:
                hit_docs = docs[docs["title"].astype(str).str.contains(rx)]
                st.dataframe(hit_docs.head(50))
            st.write("Text-Units mit Treffer im Text:")
            if not tus.empty:
                hit_tu = tus[tus["text"].astype(str).str.contains(rx)]
                st.dataframe(hit_tu[["id","human_readable_id","text"]].head(50))
        except Exception as ex:
            st.error(f"Fehler beim Keyword-Test: {ex}")

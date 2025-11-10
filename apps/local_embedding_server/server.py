# -*- coding: utf-8 -*-
"""
Local OpenAI-compatible Embeddings server for jinaai/jina-embeddings-v2-base-de.

- Serves POST /v1/embeddings with the OpenAI embeddings API schema
- Loads the model via sentence-transformers
- Default model: jinaai/jina-embeddings-v2-base-de (768 dims)

Run (without adding deps to the project):
  uv run --with fastapi --with uvicorn --with "sentence-transformers>=2.6" \
    --with "torch>=2.2" \
    uvicorn apps.local_embedding_server.server:app --host 127.0.0.1 --port 8000

Or add dependencies once to your project and then run uvicorn directly.
"""
from __future__ import annotations

import os
from typing import Any, List

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import traceback

# Lazy import to speed up startup error messages
from sentence_transformers import SentenceTransformer

DEFAULT_MODEL_ID = os.environ.get("MODEL_ID", "jinaai/jina-embeddings-v2-base-de")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))
EMBED_DEVICE = os.environ.get("EMBED_DEVICE", "cpu")

app = FastAPI(title="Local Embeddings Server", version="1.0.0")


class EmbeddingRequest(BaseModel):
    input: str | List[str]
    model: str | None = Field(default=None)


class EmbeddingItem(BaseModel):
    object: str = "embedding"
    embedding: List[float]
    index: int


class EmbeddingUsage(BaseModel):
    prompt_tokens: int = 0
    total_tokens: int = 0


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: List[EmbeddingItem]
    model: str
    usage: EmbeddingUsage = Field(default_factory=EmbeddingUsage)


# Global model instance (loaded on first request)
_model: SentenceTransformer | None = None


def get_model(model_id: str = DEFAULT_MODEL_ID) -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(model_id, device=EMBED_DEVICE)
    return _model


@app.get("/")
def health() -> dict[str, Any]:
    return {"status": "ok", "model": DEFAULT_MODEL_ID, "dim": EMBED_DIM, "device": EMBED_DEVICE}


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
def create_embeddings(req: EmbeddingRequest):
    try:
        # Always use the server's configured model; ignore client-provided model to avoid HF name mismatches
        model_id = DEFAULT_MODEL_ID
        texts: List[str]
        if isinstance(req.input, str):
            texts = [req.input]
        else:
            texts = req.input

        model = get_model(model_id)
        vectors = model.encode(texts, normalize_embeddings=True)

        model = get_model(model_id)
        vectors = model.encode(texts, normalize_embeddings=True)

        # Ensure lists of floats
        if hasattr(vectors, "tolist"):
            vectors = vectors.tolist()

        # Safety: model might return a single vector shape
        if isinstance(vectors[0], float):
            vectors = [vectors]

        data = [EmbeddingItem(embedding=vec, index=i) for i, vec in enumerate(vectors)]

        # (Optional) Validate dimension
        for item in data:
            if len(item.embedding) != EMBED_DIM:
                # Do not fail, but include info in usage
                pass

        return EmbeddingResponse(data=data, model=model_id)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "trace": traceback.format_exc(),
            },
        )

"""Ollama-first local LLM/embeddings (RTX 4070). Degrades gracefully.

Used for optional semantic dedup (bge-m3 embeddings) and optional file
categorization (qwen2.5). If Ollama is unreachable, callers skip these
features rather than fail the pipeline.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .logging_conf import get_logger

log = get_logger("ollama")


class OllamaClient:
    def __init__(self, host: str, embed_model: str, llm_model: str, timeout: float = 30.0):
        self.host = host.rstrip("/")
        self.embed_model = embed_model
        self.llm_model = llm_model
        self.timeout = timeout

    def _post(self, endpoint: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.host}{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.host}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3.0):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def embed(self, text: str) -> list[float] | None:
        try:
            data = self._post("/api/embeddings", {"model": self.embed_model, "prompt": text})
            return data.get("embedding")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("Embedding indisponible (%s) -- dédup sémantique désactivée", e)
            return None

    def classify(self, text: str, categories: list[str]) -> str | None:
        prompt = (
            "Classe ce fichier dans UNE seule catégorie parmi: "
            + ", ".join(categories)
            + ".\nRéponds uniquement par le nom exact de la catégorie.\n\nFichier:\n"
            + text
        )
        try:
            data = self._post("/api/generate", {
                "model": self.llm_model, "prompt": prompt, "stream": False,
                "options": {"temperature": 0},
            })
            ans = (data.get("response") or "").strip()
            for c in categories:
                if c.lower() in ans.lower():
                    return c
            return None
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("LLM indisponible (%s) -- classification LLM désactivée", e)
            return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

"""Thin Ollama client for local LLM calls (no cloud API).

Used by agents for structured tasks (e.g. the Organiser's column mapping). Calls
are best-effort: any failure/timeout returns None so callers can fall back to
deterministic logic instead of breaking the request.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import requests

from .. import config


def _parse_json(content: str) -> Optional[dict[str, Any]]:
    """Parse a JSON object from model output, tolerating ```json fences / stray text."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return None


def is_available(timeout: float = 5.0) -> bool:
    """True if the Ollama server answers; used to skip slow calls when it's down."""
    try:
        resp = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=timeout)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def chat_json(
    system: str,
    user: str,
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    think: bool = False,
) -> Optional[dict[str, Any]]:
    """Ask the model for a JSON object. Returns the parsed dict, or None on failure.

    `think=False` suppresses reasoning tokens on qwen3 thinking models for speed.
    """
    payload = {
        "model": model or config.OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "think": think,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        resp = requests.post(
            f"{config.OLLAMA_URL}/api/chat",
            json=payload,
            timeout=timeout or config.OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        if not content:
            return None
        return _parse_json(content)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return None

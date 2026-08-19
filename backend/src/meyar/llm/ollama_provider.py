import json
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from meyar.extraction.prompts import SYSTEM_PROMPT, build_user_prompt
from meyar.extraction.view import ProfessionalDocumentView
from meyar.llm.provider import ModelSchemaInvalidError, ModelTimeoutError, ModelUnavailableError
from meyar.schemas.candidate_profile import CandidateProfileExtraction

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class OllamaLLMProvider:
    """Local-only Ollama provider. Candidate document content never
    leaves this machine — the base_url is required to be a loopback
    address; anything else is rejected at construction time. See
    docs/MASTER_SPEC.md §16 and Slice 4 spec §3."""

    def __init__(self, *, base_url: str, model: str, timeout_seconds: float) -> None:
        if urlparse(base_url).hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"MEYAR_OLLAMA_BASE_URL ({base_url}) must be a loopback address "
                "(127.0.0.1/localhost) — candidate content must never leave this "
                "machine. See docs/MASTER_SPEC.md §16."
            )
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                tags = [m.get("name") for m in resp.json().get("models", [])]
                return {
                    "reachable": True,
                    "model": self._model,
                    "model_available": self._model in tags,
                }
        except httpx.HTTPError:
            return {"reachable": False, "model": self._model, "model_available": False}

    async def extract_candidate_profile(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateProfileExtraction, str]:
        schema = CandidateProfileExtraction.model_json_schema()
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(view)},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"Ollama request timed out after {self._timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"Ollama unreachable: {exc}") from exc

        if resp.status_code != 200:
            raise ModelUnavailableError(f"Ollama returned HTTP {resp.status_code}.")

        content = resp.json().get("message", {}).get("content", "")
        try:
            data = json.loads(content)
            extraction = CandidateProfileExtraction.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ModelSchemaInvalidError(
                f"Model output failed structured-schema validation: {exc}"
            ) from exc

        return extraction, self._model

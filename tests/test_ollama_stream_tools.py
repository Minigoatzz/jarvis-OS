# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — capture des tool_calls en streaming pour Ollama (JRV-LLM-002).

Reproduit un bug observé en usage réel : en mode local (LLM_PROVIDER=local),
AUCUN outil ne s'exécutait en conversation normale.

Mécanisme : Agent.start_routing_stream() choisit son chemin avec
`hasattr(self._llm, "stream_with_capture")`. OllamaProvider ne définissait pas
cette méthode, donc l'agent retombait sur sa branche « provider sans outil »,
qui appelle complete(stream=True) SANS passer `tools` — le payload envoyé à
Ollama n'avait alors aucune clé "tools", et la capture rendue au gateway valait
None. Le modèle ne voyait les outils que décrits en toutes lettres dans le
prompt système (les skills-vues documentent `show_view(action="show", ...)`) et
recopiait la notation en texte. Résultat visible pour l'utilisateur :
« spotify_control(action="pause") » affiché comme réponse, et rien ne se passe.

tool_loop() implémentait pourtant déjà l'appel natif, en non-streaming — les
deux moitiés n'avaient jamais été reliées.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from unittest.mock import patch

import pytest


class _FakeStreamResponse:
    """Réponse httpx streamée factice : rejoue des lignes NDJSON façon Ollama."""

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeClient:
    """Client httpx factice dont .stream() est un context manager asynchrone."""

    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response
        self.sent_payloads: list[dict] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def stream(
        self, _method: str, _url: str, json: dict | None = None  # noqa: A002
    ) -> AbstractAsyncContextManager[_FakeStreamResponse]:
        self.sent_payloads.append(json or {})
        outer = self

        class _Ctx:
            async def __aenter__(self) -> _FakeStreamResponse:
                return outer._response

            async def __aexit__(self, *_: object) -> bool:
                return False

        return _Ctx()


def _chunk(content: str = "", tool_calls: list[dict] | None = None, done: bool = False) -> str:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return json.dumps({"model": "qwen3:14b", "message": message, "done": done})


def _provider():  # noqa: ANN202
    from jarvis.providers.llm.local import OllamaProvider

    return OllamaProvider()


async def _drain(stream: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in stream])


# ── Le garde-fou exact utilisé par Agent.start_routing_stream ────────────────


def test_ollama_exposes_stream_with_capture() -> None:
    """C'est littéralement le test que fait agent.py pour router vers les outils.

    S'il échoue, l'agent retombe silencieusement sur la branche sans outil et
    plus aucun outil ne s'exécute en mode local — sans la moindre erreur logguée.
    """
    assert hasattr(_provider(), "stream_with_capture")


# ── La régression de fond : les tools doivent partir dans le payload ─────────


@pytest.mark.asyncio
async def test_stream_with_capture_sends_tools_in_payload() -> None:
    from jarvis.capabilities.tools.base import Tool  # noqa: F401  (garde l'import symétrique)

    provider = _provider()
    tools = [
        {
            "name": "spotify_control",
            "description": "Contrôle la lecture Spotify",
            "input_schema": {
                "type": "object",
                "properties": {"action": {"type": "string"}},
                "required": ["action"],
            },
        }
    ]
    client = _FakeClient(_FakeStreamResponse([_chunk(content="ok", done=True)]))

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=client):
        stream, _capture = provider.stream_with_capture(
            messages=[{"role": "user", "content": "pause ma musique"}],
            system="tu es Jarvis",
            tools=tools,
        )
        await _drain(stream)

    assert client.sent_payloads, "aucune requête envoyée"
    payload = client.sent_payloads[0]
    assert "tools" in payload, "les schémas d'outils n'ont pas été envoyés à Ollama"
    assert payload["tools"][0]["function"]["name"] == "spotify_control"


# ── La capture elle-même ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_with_capture_collects_tool_call() -> None:
    """Le cas réel : « pause ma musique » -> un tool_call capturé, pas du texte."""
    provider = _provider()
    lines = [
        _chunk(content=""),
        _chunk(
            tool_calls=[
                {"function": {"name": "spotify_control", "arguments": {"action": "pause"}}}
            ]
        ),
        _chunk(done=True),
    ]
    client = _FakeClient(_FakeStreamResponse(lines))

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=client):
        stream, capture = provider.stream_with_capture(
            messages=[{"role": "user", "content": "pause ma musique"}], system="", tools=[]
        )
        await _drain(stream)

    assert len(capture.calls) == 1
    call_id, name, args = capture.calls[0]
    assert name == "spotify_control"
    assert args == {"action": "pause"}
    assert call_id, "un id non vide est requis — le gateway s'en sert pour apparier le résultat"
    assert capture.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_stream_with_capture_parses_string_arguments() -> None:
    """Certains modèles renvoient `arguments` en chaîne JSON plutôt qu'en objet."""
    provider = _provider()
    lines = [
        _chunk(
            tool_calls=[
                {"function": {"name": "show_view", "arguments": '{"action": "show"}'}}
            ]
        ),
        _chunk(done=True),
    ]
    client = _FakeClient(_FakeStreamResponse(lines))

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=client):
        stream, capture = provider.stream_with_capture(messages=[], system="", tools=[])
        await _drain(stream)

    assert capture.calls[0][2] == {"action": "show"}


@pytest.mark.asyncio
async def test_stream_text_still_works_and_think_is_filtered() -> None:
    """Non-régression : le texte continue de streamer, <think> reste filtré."""
    provider = _provider()
    lines = [
        _chunk(content="<think>je reflechis</think>"),
        _chunk(content="Bonjour"),
        _chunk(content=" Minigoatz", done=True),
    ]
    client = _FakeClient(_FakeStreamResponse(lines))

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=client):
        stream, capture = provider.stream_with_capture(messages=[], system="", tools=[])
        text = await _drain(stream)

    assert "je reflechis" not in text
    assert text == "Bonjour Minigoatz"
    assert capture.calls == []


@pytest.mark.asyncio
async def test_plain_stream_without_capture_unaffected() -> None:
    """complete(stream=True) passe toujours capture=None — aucun changement."""
    provider = _provider()
    client = _FakeClient(_FakeStreamResponse([_chunk(content="salut", done=True)]))

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=client):
        result = await provider.complete(messages=[], system="", stream=True)
        text = await _drain(result)  # type: ignore[arg-type]

    assert text == "salut"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── num_ctx : fenêtre de contexte demandée explicitement ────────────────────


@pytest.mark.asyncio
async def test_payload_requests_explicit_num_ctx() -> None:
    """Sans num_ctx, Ollama applique son défaut VRAM (4096 sur 16 Go) et tronque
    par le début — donc les schémas d'outils, silencieusement."""
    from jarvis.kernel.settings import settings

    provider = _provider()
    client = _FakeClient(_FakeStreamResponse([_chunk(content="ok", done=True)]))

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=client):
        stream, _ = provider.stream_with_capture(messages=[], system="", tools=[])
        await _drain(stream)

    options = client.sent_payloads[0]["options"]
    assert options["num_ctx"] == settings.ollama_num_ctx
    assert options["num_ctx"] >= 8192, "trop court pour le prompt système + les outils"
    assert options["temperature"] == 0.7, "le réglage existant ne doit pas être écrasé"


def test_num_ctx_is_configurable_from_env() -> None:
    """Réglable via OLLAMA_NUM_CTX dans .env, sans toucher au serveur Ollama."""
    from jarvis.kernel.settings import Settings

    assert "ollama_num_ctx" in Settings.model_fields

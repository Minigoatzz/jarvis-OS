# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — repli automatique sur le champ "think" (JRV-LLM-002).

Reproduit le bug observé en usage réel : un serveur/modèle Ollama qui ne
supporte pas la capability "thinking" répond 400 dès que le payload contient
la clé "think". Ces tests couvrent les trois points d'appel HTTP du provider
(complete, _stream, tool_loop) et vérifient que :
  1. un 400 mentionnant "think" déclenche un unique retry sans ce champ, qui
     réussit ;
  2. un 400 sans rapport avec "think" continue de se propager normalement
     (pas de masquage silencieux, pas de retry inutile).

Utilise de vrais objets httpx.Response (plutôt que des MagicMock) pour que
raise_for_status()/.text/.json() se comportent exactement comme en
production — c'est ce qui a révélé, lors de l'écriture de ces tests, que la
première version de _post_chat cassait la suite existante en inspectant
response.status_code directement (incompatible avec les mocks non configurés
de test_ollama_tools.py).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Retourne la météo d'une ville.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}

_REQUEST = httpx.Request("POST", "http://ollama.local:11434/api/chat")


def _real_response(
    status_code: int, json_data: dict | None = None, text: str = ""
) -> httpx.Response:
    """Construit un vrai httpx.Response (pas un mock) pour un comportement fidèle."""
    content = json.dumps(json_data).encode() if json_data is not None else text.encode()
    return httpx.Response(status_code=status_code, content=content, request=_REQUEST)


def _text_response(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}}


def _make_httpx_post_mock(*responses: httpx.Response) -> tuple[MagicMock, AsyncMock]:
    """Comme dans test_ollama_tools.py : mock_ctx pour httpx.AsyncClient(...), post en série."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=list(responses))

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx, mock_client


def _stream_cm(resp: httpx.Response) -> AsyncMock:
    """Context manager async simulant celui retourné par client.stream(...)."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_httpx_stream_mock(*responses: httpx.Response) -> tuple[MagicMock, AsyncMock]:
    """mock_ctx pour httpx.AsyncClient(...) dont .stream(...) renvoie les réponses en série.

    client.stream(...) n'est PAS une coroutine en httpx réel (elle renvoie
    directement un context manager synchrone) : on la mocke donc avec
    MagicMock (pas AsyncMock) pour rester fidèle à l'API réelle.
    """
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(side_effect=[_stream_cm(r) for r in responses])

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx, mock_client


def _ndjson(*chunks: dict) -> bytes:
    return ("\n".join(json.dumps(c) for c in chunks) + "\n").encode()


# ── complete() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_think_400_retries_and_succeeds() -> None:
    """Un 400 mentionnant 'think' déclenche un retry sans ce champ, qui réussit."""
    from jarvis.providers.llm.local import OllamaProvider

    resp1 = _real_response(400, text='{"error":"json: unknown field \\"think\\""}')
    resp2 = _real_response(200, json_data=_text_response("Bonjour !"))
    mock_ctx, mock_client = _make_httpx_post_mock(resp1, resp2)

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=mock_ctx):
        provider = OllamaProvider()
        result = await provider.complete(
            messages=[{"role": "user", "content": "Salut"}],
            system="Tu es Jarvis.",
        )

    assert result == "Bonjour !"
    assert mock_client.post.call_count == 2

    first_payload = mock_client.post.call_args_list[0].kwargs["json"]
    second_payload = mock_client.post.call_args_list[1].kwargs["json"]
    assert "think" in first_payload
    assert "think" not in second_payload
    # Le reste du payload (modèle, messages, options) doit être préservé.
    assert second_payload["model"] == first_payload["model"]
    assert second_payload["messages"] == first_payload["messages"]


@pytest.mark.asyncio
async def test_complete_unrelated_400_propagates() -> None:
    """Un 400 sans rapport avec 'think' continue de se propager, sans retry inutile."""
    from jarvis.providers.llm.local import OllamaProvider

    resp1 = _real_response(400, text='{"error":"model \\"qwen3:14b\\" not found"}')
    mock_ctx, mock_client = _make_httpx_post_mock(resp1)

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=mock_ctx):
        provider = OllamaProvider()
        with pytest.raises(httpx.HTTPStatusError):
            await provider.complete(
                messages=[{"role": "user", "content": "Salut"}],
                system="Tu es Jarvis.",
            )

    # Un seul appel : pas de retry pour une erreur qui n'a rien à voir avec 'think'.
    assert mock_client.post.call_count == 1


# ── tool_loop() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_loop_think_400_retries_and_succeeds() -> None:
    """tool_loop : même repli 'think', en préservant le champ 'tools' dans le retry."""
    from jarvis.providers.llm.local import OllamaProvider

    resp1 = _real_response(400, text='{"error":"invalid option: think"}')
    resp2 = _real_response(200, json_data=_text_response("Il fait beau."))
    mock_ctx, mock_client = _make_httpx_post_mock(resp1, resp2)

    async def mock_executor(name: str, args: dict) -> str:
        return "ne devrait pas être appelé"

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=mock_ctx):
        provider = OllamaProvider()
        result = await provider.tool_loop(
            messages=[{"role": "user", "content": "Quel temps ?"}],
            system="sys",
            tools=[_WEATHER_TOOL],
            tool_executor=mock_executor,
        )

    assert result == "Il fait beau."
    assert mock_client.post.call_count == 2
    second_payload = mock_client.post.call_args_list[1].kwargs["json"]
    assert "think" not in second_payload
    assert "tools" in second_payload  # les tools ne doivent pas disparaître avec le retry


@pytest.mark.asyncio
async def test_tool_loop_unrelated_400_propagates() -> None:
    """tool_loop : une erreur 400 sans rapport avec 'think' se propage toujours."""
    from jarvis.providers.llm.local import OllamaProvider

    resp1 = _real_response(500, text="internal server error")
    mock_ctx, mock_client = _make_httpx_post_mock(resp1)

    async def mock_executor(name: str, args: dict) -> str:
        return "ne devrait pas être appelé"

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=mock_ctx):
        provider = OllamaProvider()
        with pytest.raises(httpx.HTTPStatusError):
            await provider.tool_loop(
                messages=[{"role": "user", "content": "Quel temps ?"}],
                system="sys",
                tools=[_WEATHER_TOOL],
                tool_executor=mock_executor,
            )

    assert mock_client.post.call_count == 1


# ── _stream() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_think_400_retries_and_succeeds() -> None:
    """_stream : un 400 'think' sur le flux initial déclenche un nouveau flux sans ce champ."""
    from jarvis.providers.llm.local import OllamaProvider

    resp1 = _real_response(400, text='{"error":"unknown field \\"think\\""}')
    ndjson = _ndjson(
        {"message": {"content": "Sa"}, "done": False},
        {"message": {"content": "lut"}, "done": False},
        {"message": {"content": ""}, "done": True},
    )
    resp2 = httpx.Response(status_code=200, content=ndjson, request=_REQUEST)
    mock_ctx, mock_client = _make_httpx_stream_mock(resp1, resp2)

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=mock_ctx):
        provider = OllamaProvider()
        payload = provider._payload(
            messages=[{"role": "user", "content": "Salut"}], system="sys", stream=True
        )
        chunks: list[str] = []
        async for chunk in provider._stream(payload):
            chunks.append(chunk)

    assert "".join(chunks) == "Salut"
    assert mock_client.stream.call_count == 2
    second_payload = mock_client.stream.call_args_list[1].kwargs["json"]
    assert "think" not in second_payload


@pytest.mark.asyncio
async def test_stream_unrelated_400_propagates() -> None:
    """_stream : une erreur 400 sans rapport avec 'think' se propage, sans retry inutile."""
    from jarvis.providers.llm.local import OllamaProvider

    resp1 = _real_response(400, text='{"error":"model not found"}')
    mock_ctx, mock_client = _make_httpx_stream_mock(resp1)

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=mock_ctx):
        provider = OllamaProvider()
        payload = provider._payload(
            messages=[{"role": "user", "content": "Salut"}], system="sys", stream=True
        )
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in provider._stream(payload):
                pass

    assert mock_client.stream.call_count == 1

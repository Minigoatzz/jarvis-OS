# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — ce que l'utilisateur LIT quand un outil s'exécute.

Trois bugs observés en usage réel, tous sur le même tour de conversation
(« pause ma musique », qwen3:14b en mode local) :

1. La réponse s'affichait EN DOUBLE — « C'est pausé. C'est pausé. ». Le premier
   jet du modèle répond avant que l'outil ait tourné, puis la synthèse
   post-outil redit la même chose. Les deux partaient dans la même bulle, et
   `session.add_message("assistant", full)` (chat.py, websocket.py) stockait la
   concaténation : au tour suivant le modèle relisait sa propre répétition.

2. La notation d'appel s'affichait telle quelle — `spotify_control(action="pause")`
   en plein milieu de la conversation. Demande explicite de l'utilisateur :
   « je ne veux pas qu'il me dise la commande, je veux qu'il me dise "ok je mets
   ta musique en pause" ».

3. Le modèle écrit parfois l'appel en JSON plutôt qu'en notation d'appel :
   `{"name": "spotify_control", "arguments": {"action": "pause"}}`. Cette forme
   traversait l'analyseur sans laisser de trace — aucun outil exécuté, aucune
   erreur journalisée, la demande disparaissait en silence. Les deux formes ont
   été produites par le MÊME modèle sur le MÊME prompt, à deux exécutions
   consécutives de la campagne d'ablation.

Le correctif tient en un seul endroit — le gateway (L2) — donc les quatre
interfaces (websocket, chat HTTP, voix, gesture) en héritent sans modification :
elles stockent et affichent exactement ce que `_pipe()` leur rend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.capabilities.tools.registry import ToolRegistry
from jarvis.engine.agent import Agent
from jarvis.engine.background.notifications import NotificationQueue
from jarvis.engine.gateway import Gateway
from jarvis.engine.session import SessionManager
from jarvis.kernel.settings import settings

_CALL_PAREN = 'spotify_control(action="pause")'
_CALL_JSON = '{"name": "spotify_control", "arguments": {"action": "pause"}}'


class _SpotifyTool(Tool):
    name = "spotify_control"
    description = "Contrôle la lecture Spotify (pause, play, next)."
    input_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, **kwargs: object) -> ToolResult:
        self.calls.append(dict(kwargs))
        return ToolResult(content=f"Spotify : {kwargs.get('action')} OK.")


class _FakeLLM:
    """Rejoue un premier jet figé, puis une synthèse figée.

    `stream_with_capture` ne peuple JAMAIS la capture : c'est exactement le
    comportement d'Ollama ici — le modèle écrit l'appel dans le texte.
    `seen_synth_messages` garde les messages du 2e appel pour vérifier ce qui est
    RENDU AU MODÈLE, distinct de ce qui est rendu à l'utilisateur.
    """

    def __init__(self, first_pass: str, synth: str = "C'est en pause.") -> None:
        self._first = first_pass
        self._synth = synth
        self.seen_synth_messages: list[dict] = []
        self.seen_synth_system = ""

    @property
    def supports_tools(self) -> bool:
        return True

    def stream_with_capture(
        self, messages: list[dict], system: str, tools: list[dict] | None = None
    ) -> tuple[AsyncIterator[str], object]:
        from jarvis.kernel.schemas import ToolCapture

        capture = ToolCapture()

        async def _gen() -> AsyncIterator[str]:
            for word in self._first.split(" "):
                yield word + " "

        return _gen(), capture

    async def tool_loop(self, *a: object, **k: object) -> str:
        raise AssertionError("tool_loop ne doit plus être sollicité")

    async def complete(self, *a: object, **k: object) -> AsyncIterator[str]:
        self.seen_synth_messages = list(k.get("messages") or (a[0] if a else []))
        self.seen_synth_system = str(k.get("system") or "")
        synth = self._synth

        async def _gen() -> AsyncIterator[str]:
            yield synth

        return _gen()


def _agent(llm: _FakeLLM) -> tuple[Agent, _SpotifyTool]:
    tool = _SpotifyTool()
    registry = ToolRegistry()
    registry.register(tool)
    return Agent(settings=settings, llm=llm, tool_registry=registry), tool  # type: ignore[arg-type]


def _gateway(
    first_pass: str, synth: str = "C'est en pause."
) -> tuple[Gateway, _SpotifyTool, _FakeLLM]:
    llm = _FakeLLM(first_pass, synth)
    agent, tool = _agent(llm)
    gateway = Gateway(
        session_manager=SessionManager(),
        agent=agent,
        notifications=NotificationQueue(),
        worker=None,  # type: ignore[arg-type]
    )
    return gateway, tool, llm


async def _run(gateway: Gateway, message: str) -> str:
    _session, _route, response = await gateway.handle(message, stream=True)
    assert not isinstance(response, str)
    return "".join([chunk async for chunk in response])


# ── Bug 3 : la notation JSON doit être reconnue comme un appel ───────────────


def test_json_notation_is_parsed_as_a_tool_call() -> None:
    """La forme que l'analyseur laissait passer — demande perdue en silence."""
    agent, _tool = _agent(_FakeLLM(""))

    calls = agent.extract_text_tool_calls(f"[CF]{_CALL_JSON}")

    assert len(calls) == 1
    _cid, name, args = calls[0]
    assert name == "spotify_control"
    assert args == {"action": "pause"}


def test_json_notation_accepts_parameters_key() -> None:
    """Certains modèles écrivent `parameters` au lieu de `arguments`."""
    agent, _tool = _agent(_FakeLLM(""))

    calls = agent.extract_text_tool_calls(
        '{"name": "spotify_control", "parameters": {"action": "next"}}'
    )

    assert calls[0][2] == {"action": "next"}


def test_json_notation_accepts_arguments_as_json_string() -> None:
    """`arguments` peut arriver en chaîne JSON plutôt qu'en objet."""
    agent, _tool = _agent(_FakeLLM(""))

    calls = agent.extract_text_tool_calls(
        '{"name": "spotify_control", "arguments": "{\\"action\\": \\"play\\"}"}'
    )

    assert calls[0][2] == {"action": "play"}


def test_json_notation_inside_a_code_fence_is_found() -> None:
    """Le modèle encadre souvent son JSON d'une clôture markdown."""
    agent, _tool = _agent(_FakeLLM(""))

    calls = agent.extract_text_tool_calls(f"Voilà :\n```json\n{_CALL_JSON}\n```\n")

    assert len(calls) == 1
    assert calls[0][1] == "spotify_control"


def test_unregistered_name_in_json_is_ignored() -> None:
    """Même garde-fou que pour la notation d'appel : seuls les outils inscrits."""
    agent, _tool = _agent(_FakeLLM(""))

    assert agent.extract_text_tool_calls('{"name": "rm_rf_slash", "arguments": {}}') == []


def test_prose_with_braces_is_not_a_tool_call() -> None:
    """Garde-fou contre une détection trop large."""
    agent, _tool = _agent(_FakeLLM(""))

    assert agent.extract_text_tool_calls("Le set {a, b, c} est fini. {pas du json}") == []


def test_nested_json_is_not_executed_twice() -> None:
    """Un objet imbriqué ne doit pas produire un second appel."""
    agent, _tool = _agent(_FakeLLM(""))

    calls = agent.extract_text_tool_calls(
        '{"name": "spotify_control", "arguments": {"action": "pause", "opts": {"fade": 1}}}'
    )

    assert len(calls) == 1


# ── Le scanner de parenthèses et les guillemets échappés ────────────────────


def test_escaped_quotes_inside_arguments_survive() -> None:
    """Sortie réellement produite par qwen3:14b pendant l'ablation.

    Le scanner « sautait » l'antislash sans consommer le caractère suivant : le
    `\\"` refermait la chaîne trop tôt et les arguments partaient en morceaux.
    """
    agent, _tool = _agent(_FakeLLM(""))
    registry = agent._tool_registry  # type: ignore[attr-defined]
    registry.register(_EchoCli())

    text = "[CF] execute_cli(command=\"osascript -e 'tell application \\\"Spotify\\\" to pause'\")"
    calls = agent.extract_text_tool_calls(text)

    assert len(calls) == 1
    # Les `\"` sont dés-échappés : c'est bien la commande que le shell doit voir.
    assert calls[0][2]["command"] == "osascript -e 'tell application \"Spotify\" to pause'"


class _EchoCli(Tool):
    name = "execute_cli"
    description = "Exécute une commande shell."
    input_schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    async def execute(self, **kwargs: object) -> ToolResult:
        return ToolResult(content=str(kwargs.get("command")))


# ── strip_text_tool_calls ───────────────────────────────────────────────────


def test_strip_removes_paren_notation_but_keeps_prose() -> None:
    agent, _tool = _agent(_FakeLLM(""))

    assert agent.strip_text_tool_calls(f"Je mets ça en pause. {_CALL_PAREN}") == (
        "Je mets ça en pause."
    )


def test_strip_removes_json_notation() -> None:
    agent, _tool = _agent(_FakeLLM(""))

    assert agent.strip_text_tool_calls(f"Ok. {_CALL_JSON}") == "Ok."


def test_strip_leaves_a_plain_answer_untouched() -> None:
    agent, _tool = _agent(_FakeLLM(""))

    assert agent.strip_text_tool_calls("Il est 22h53.") == "Il est 22h53."


# ── Bug 2 : la notation ne doit jamais atteindre l'utilisateur ──────────────


@pytest.mark.asyncio
async def test_user_never_sees_the_raw_tool_call() -> None:
    gateway, tool, _llm = _gateway(f'[CF] Je mets ça en pause. {_CALL_PAREN}')

    out = await _run(gateway, "pause ma musique")

    assert tool.calls == [{"action": "pause"}], "l'outil doit quand même s'exécuter"
    assert "spotify_control(" not in out
    assert "action=" not in out


@pytest.mark.asyncio
async def test_user_never_sees_the_raw_json_call() -> None:
    gateway, tool, _llm = _gateway(f"[CF] Ok. {_CALL_JSON}")

    out = await _run(gateway, "pause ma musique")

    assert tool.calls == [{"action": "pause"}]
    assert '"name"' not in out
    assert "spotify_control" not in out


# ── Bug 1 : plus de réponse en double ───────────────────────────────────────


@pytest.mark.asyncio
async def test_answer_is_not_repeated_twice() -> None:
    """Le symptôme exact rapporté : « C'est pausé. C'est pausé. »"""
    gateway, _tool, _llm = _gateway(
        f"[CF] C'est pausé. {_CALL_PAREN}", synth="C'est pausé."
    )

    out = await _run(gateway, "pause ma musique")

    assert out.count("C'est pausé.") == 1, f"réponse dupliquée : {out!r}"


@pytest.mark.asyncio
async def test_synthesis_is_what_reaches_the_user() -> None:
    gateway, _tool, _llm = _gateway(
        f"[CF] Je regarde. {_CALL_PAREN}", synth="Voilà, c'est en pause."
    )

    out = await _run(gateway, "pause ma musique")

    assert "Voilà, c'est en pause." in out
    assert "Je regarde." not in out, "le premier jet ne doit pas s'ajouter à la synthèse"


# ── Non-régression : rien ne doit être avalé quand aucun outil ne tourne ────


@pytest.mark.asyncio
async def test_cf_without_any_tool_call_still_answers() -> None:
    """Route CF mais aucun appel : le premier jet EST la réponse, il doit sortir."""
    gateway, tool, _llm = _gateway("[CF] Je n'ai pas trouvé de lecteur actif.")

    out = await _run(gateway, "pause ma musique")

    assert tool.calls == []
    assert "Je n'ai pas trouvé de lecteur actif." in out


@pytest.mark.asyncio
async def test_instant_route_still_streams_its_answer() -> None:
    """Route I : aucun différé, la réponse conversationnelle passe telle quelle."""
    gateway, tool, _llm = _gateway("[I] Il est 22h53.")

    out = await _run(gateway, "quelle heure il est")

    assert tool.calls == []
    assert "Il est 22h53." in out


# ── Ce qui est rendu au MODÈLE (et donc au transcript) ──────────────────────


@pytest.mark.asyncio
async def test_model_is_not_fed_back_its_own_call_notation() -> None:
    """La boucle de renforcement : relire sa propre notation l'incite à recommencer."""
    gateway, _tool, llm = _gateway(f"[CF] Je mets ça en pause. {_CALL_PAREN}")

    await _run(gateway, "pause ma musique")

    assistant_blocks = [
        block
        for message in llm.seen_synth_messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert assistant_blocks, "la synthèse doit recevoir le texte du premier jet"
    for block in assistant_blocks:
        assert "spotify_control(" not in block["text"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── L'outil échoue : Jarvis ne doit pas annoncer un succès ──────────────────
#
# Symptôme réel : « joue l'album unplugged d'Alice in Chains » -> « C'est lancé. »
# et la musique ne bouge pas. Spotify rend pourtant une erreur explicite
# (« Playlist trouvée mais impossible de lancer (404) », typiquement aucun
# appareil actif). ToolRegistry.call() la préfixe bien de son code JRV, mais la
# synthèse ne distinguait pas ce texte d'un résultat normal.


class _FailingSpotify(Tool):
    name = "spotify_control"
    description = "Contrôle la lecture Spotify (pause, play, next)."
    input_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "query": {"type": "string"}},
        "required": ["action"],
    }

    async def execute(self, **kwargs: object) -> ToolResult:
        return ToolResult(
            content="Playlist trouvée (Unplugged) mais impossible de lancer (404).",
            is_error=True,
        )


def _gateway_with(tool: Tool, first_pass: str, synth: str) -> tuple[Gateway, _FakeLLM]:
    registry = ToolRegistry()
    registry.register(tool)
    llm = _FakeLLM(first_pass, synth)
    agent = Agent(settings=settings, llm=llm, tool_registry=registry)  # type: ignore[arg-type]
    return (
        Gateway(
            session_manager=SessionManager(),
            agent=agent,
            notifications=NotificationQueue(),
            worker=None,  # type: ignore[arg-type]
        ),
        llm,
    )


def test_jrv_prefix_is_recognised_as_a_failure() -> None:
    from jarvis.engine.agent import _is_tool_error

    assert _is_tool_error("[JRV-TOL-004] Playlist trouvée mais impossible de lancer (404).")
    assert not _is_tool_error("Lecture de la playlist « Unplugged ».")
    assert not _is_tool_error("[INFO] rien à signaler")


@pytest.mark.asyncio
async def test_failed_tool_is_flagged_is_error_in_the_synthesis_blocks() -> None:
    gateway, llm = _gateway_with(
        _FailingSpotify(),
        '[CF] spotify_control(action="search_playlist", query="unplugged alice in chains")',
        synth="Je n'ai pas pu lancer la playlist.",
    )

    await _run(gateway, "joue l'album unplugged d'alice in chains")

    blocks = [
        block
        for message in llm.seen_synth_messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert blocks, "la synthèse doit recevoir un tool_result"
    assert all(block["is_error"] for block in blocks), "l'échec n'est pas signalé au modèle"


@pytest.mark.asyncio
async def test_failed_tool_adds_a_do_not_claim_success_directive() -> None:
    gateway, llm = _gateway_with(
        _FailingSpotify(),
        '[CF] spotify_control(action="search_playlist", query="unplugged alice in chains")',
        synth="Je n'ai pas pu lancer la playlist.",
    )

    await _run(gateway, "joue l'album unplugged d'alice in chains")

    assert "UN OUTIL VIENT D'ÉCHOUER" in llm.seen_synth_system
    assert "N'A PAS EU LIEU" in llm.seen_synth_system


@pytest.mark.asyncio
async def test_successful_tool_gets_no_failure_directive() -> None:
    """Non-régression : un succès ne doit pas déclencher le discours d'échec."""
    gateway, _tool, llm = _gateway(f"[CF] {_CALL_PAREN}", synth="C'est en pause.")

    await _run(gateway, "pause ma musique")

    assert "UN OUTIL VIENT D'ÉCHOUER" not in llm.seen_synth_system
    blocks = [
        block
        for message in llm.seen_synth_messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert blocks and not any(block["is_error"] for block in blocks)


# ── Le modèle n'écrit AUCUN appel : relance en prompt minimal ───────────────
#
# Symptôme réel, capture du 16/09 : « pause ma musique » -> « C'est lancé. »,
# « pause la musique » -> « C'est fait. », musique toujours en lecture. Aucun
# appel écrit, aucun appel natif — donc aucun outil, et une réponse qui affirme
# le contraire. Cause : le prompt local interdisait explicitement d'écrire
# l'appel en texte, alors que c'est le SEUL transport qui fonctionne ici.


class _RetryingLLM(_FakeLLM):
    """Premier jet sans aucun appel ; la relance minimale en produit un."""

    def __init__(self, first_pass: str, forced: str, synth: str) -> None:
        super().__init__(first_pass, synth)
        self._forced = forced
        self.forced_systems: list[str] = []

    async def complete(self, *a: object, **k: object) -> AsyncIterator[str] | str:
        # stream=False => c'est la relance minimale (force_tool_call)
        if k.get("stream") is False:
            self.forced_systems.append(str(k.get("system") or ""))
            return self._forced
        return await super().complete(*a, **k)


@pytest.mark.asyncio
async def test_cf_without_any_call_is_retried_with_a_minimal_prompt() -> None:
    tool = _SpotifyTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _RetryingLLM("[CF] C'est fait.", _CALL_PAREN, "C'est en pause.")
    agent = Agent(settings=settings, llm=llm, tool_registry=registry)  # type: ignore[arg-type]
    gateway = Gateway(
        session_manager=SessionManager(),
        agent=agent,
        notifications=NotificationQueue(),
        worker=None,  # type: ignore[arg-type]
    )

    out = await _run(gateway, "pause ma musique")

    assert tool.calls == [{"action": "pause"}], "la relance n'a pas déclenché l'outil"
    assert llm.forced_systems, "force_tool_call n'a pas été appelé"
    assert "AUCUN" in llm.forced_systems[0], "le prompt minimal doit offrir une sortie"
    assert "C'est fait." not in out, "le mensonge du premier jet ne doit pas sortir"
    assert "C'est en pause." in out


@pytest.mark.asyncio
async def test_retry_returning_nothing_keeps_the_first_pass() -> None:
    """Si la relance ne trouve aucun outil, on garde la réponse d'origine."""
    tool = _SpotifyTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _RetryingLLM("[CF] Aucun lecteur actif détecté.", "AUCUN", "(jamais)")
    agent = Agent(settings=settings, llm=llm, tool_registry=registry)  # type: ignore[arg-type]
    gateway = Gateway(
        session_manager=SessionManager(),
        agent=agent,
        notifications=NotificationQueue(),
        worker=None,  # type: ignore[arg-type]
    )

    out = await _run(gateway, "pause ma musique")

    assert tool.calls == []
    assert "Aucun lecteur actif détecté." in out


def test_local_prompt_no_longer_forbids_writing_the_call() -> None:
    """Non-régression sur la cause racine : plus d'interdiction d'écrire l'appel.

    La contre-instruction correspondait à la variante E de l'ablation, mesurée
    NON à chaque tirage : elle supprimait le transport texte sans jamais obtenir
    d'appel natif en échange.
    """
    registry = ToolRegistry()
    registry.register(_SpotifyTool())
    local_settings = settings.model_copy(update={"llm_provider": "local"})
    agent = Agent(
        settings=local_settings,  # type: ignore[arg-type]
        llm=_FakeLLM(""),  # type: ignore[arg-type]
        tool_registry=registry,
    )

    system = agent._build_system()

    assert "N'écris JAMAIS l'appel en texte" not in system
    assert "ÉCRIS l'appel" in system
    assert 'spotify_control(action="pause")' in system

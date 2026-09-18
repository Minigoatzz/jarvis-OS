# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — repli tool_loop quand le modèle écrit l'appel d'outil en texte.

Reproduit, de bout en bout, le bug observé en usage réel : « pause ma musique »
affichait `spotify_control(action="pause")` dans la conversation et la musique
continuait de jouer.

Chaîne complète du bug :
1. Le prompt statique illustre les outils par des exemples du type
   `execute_cli(command="open -a 'Safari'")`. Claude les lit comme une
   indication d'intention ; qwen3:14b les recopie littéralement.
2. Le gateway ne déclenche l'exécution que sur `tool_capture.calls`, donc sur un
   tool_call NATIF. Le tag [CF] est détecté, journalisé... et jamais utilisé.
3. Résultat : aucun outil exécuté, aucune erreur journalisée, aucun `CF tools
   done` dans les logs. La demande disparaît en silence.

Ces tests utilisent le VRAI Gateway, le VRAI Agent et le VRAI ToolRegistry —
seul le provider LLM est simulé, pour rejouer exactement la sortie de qwen3.
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


class _SpotifyTool(Tool):
    """Outil réel minimal : enregistre s'il a été exécuté."""

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
    """Provider qui rejoue la sortie réelle de qwen3:14b.

    `stream_text` est streamé mot par mot sans jamais peupler la capture — le
    modèle écrit l'appel en texte. `tool_loop_calls` enregistre si le repli a
    bien atteint le function calling natif.
    """

    def __init__(
        self,
        stream_text: str,
        tool_loop_reply: str = "(tool_loop)",
        synth_reply: str = "C'est en pause.",
    ) -> None:
        self._stream_text = stream_text
        self._tool_loop_reply = tool_loop_reply
        self._synth_reply = synth_reply
        self.tool_loop_calls = 0
        self.complete_calls = 0

    @property
    def supports_tools(self) -> bool:
        return True

    def stream_with_capture(
        self, messages: list[dict], system: str, tools: list[dict] | None = None
    ) -> tuple[AsyncIterator[str], object]:
        from jarvis.kernel.schemas import ToolCapture

        capture = ToolCapture()  # reste VIDE : c'est tout le problème

        async def _gen() -> AsyncIterator[str]:
            for word in self._stream_text.split(" "):
                yield word + " "

        return _gen(), capture

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: object,
        context: str = "",
    ) -> str:
        """Ne doit PLUS être appelé : il repose sur le même function calling natif
        qui ne répond pas, et ne faisait que répéter la phrase du modèle."""
        self.tool_loop_calls += 1
        return self._tool_loop_reply

    async def complete(self, *a: object, **k: object) -> AsyncIterator[str]:
        """Sert la synthèse post-outils (2e appel LLM, streamé)."""
        self.complete_calls += 1

        async def _gen() -> AsyncIterator[str]:
            yield self._synth_reply

        return _gen()


def _build(stream_text: str) -> tuple[Gateway, _SpotifyTool, _FakeLLM]:
    tool = _SpotifyTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _FakeLLM(stream_text)
    agent = Agent(settings=settings, llm=llm, tool_registry=registry)  # type: ignore[arg-type]
    gateway = Gateway(
        session_manager=SessionManager(),
        agent=agent,
        notifications=NotificationQueue(),
        worker=None,  # type: ignore[arg-type]  # non sollicité sur ce chemin
    )
    return gateway, tool, llm


async def _run(gateway: Gateway, message: str) -> str:
    _session, _route, response = await gateway.handle(message, stream=True)
    assert not isinstance(response, str)
    return "".join([chunk async for chunk in response])


# ── Le bug exact rapporté par l'utilisateur ─────────────────────────────────


@pytest.mark.asyncio
async def test_text_written_tool_call_is_actually_executed() -> None:
    """« pause ma musique » -> l'outil s'exécute pour de vrai.

    Avant le correctif : zéro exécution, la ligne s'affichait, la musique
    continuait.
    """
    gateway, tool, llm = _build('[CF] spotify_control(action="pause")')

    await _run(gateway, "pause ma musique")

    assert tool.calls == [{"action": "pause"}], "l'outil Spotify n'a jamais été exécuté"
    assert llm.tool_loop_calls == 0, "tool_loop répète la demande sans l'exécuter — retiré"
    assert llm.complete_calls == 1, "la synthèse post-outil doit avoir lieu"


@pytest.mark.asyncio
async def test_fallback_reply_reaches_the_user() -> None:
    """La réponse du repli est bien streamée à l'utilisateur."""
    gateway, _tool, _llm = _build('[CF] spotify_control(action="pause")')

    out = await _run(gateway, "pause ma musique")

    assert "C'est en pause." in out  # la synthèse, pas la phrase répétée


@pytest.mark.asyncio
async def test_fallback_fires_without_cf_tag() -> None:
    """Un modèle local n'émet pas toujours le tag : le nom d'outil suffit."""
    gateway, tool, llm = _build('Je lance ça : spotify_control(action="pause")')

    await _run(gateway, "pause ma musique")

    assert tool.calls == [{"action": "pause"}]
    assert llm.tool_loop_calls == 0


# ── Non-régression : ne pas déclencher tool_loop à tort ─────────────────────


@pytest.mark.asyncio
async def test_plain_answer_does_not_trigger_tool_loop() -> None:
    """Une réponse conversationnelle [I] ne doit PAS coûter un appel LLM de plus."""
    gateway, tool, llm = _build("[I] Il est 22h53.")

    out = await _run(gateway, "quelle heure il est")

    assert llm.tool_loop_calls == 0, "tool_loop déclenché sans raison — latence doublée"
    assert tool.calls == []
    assert "22h53" in out


@pytest.mark.asyncio
async def test_talking_about_music_does_not_trigger_tool_loop() -> None:
    """Parler de musique sans nommer d'outil ne déclenche rien.

    Garde-fou contre une détection trop large : seuls les noms d'outils
    réellement enregistrés, suivis d'une parenthèse, comptent.
    """
    gateway, tool, llm = _build("[I] Tu écoutes Little Bit de Lykke Li, une pause serait dommage.")

    await _run(gateway, "c'est quoi cette chanson")

    assert llm.tool_loop_calls == 0
    assert tool.calls == []


@pytest.mark.asyncio
async def test_unknown_function_name_does_not_trigger_tool_loop() -> None:
    """Un nom de fonction non enregistré ne doit pas être pris pour un outil."""
    gateway, tool, llm = _build("[I] En Python tu ferais print(x) pour afficher.")

    await _run(gateway, "comment afficher en python")

    assert llm.tool_loop_calls == 0
    assert tool.calls == []


# ── Le détecteur, isolé ─────────────────────────────────────────────────────


def test_detector_matches_only_registered_tool_names() -> None:
    tool = _SpotifyTool()
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(settings=settings, llm=_FakeLLM(""), tool_registry=registry)  # type: ignore[arg-type]

    assert agent.mentions_tool_call_in_text('spotify_control(action="pause")')
    assert agent.mentions_tool_call_in_text("appelle spotify_control (action='next')")
    assert not agent.mentions_tool_call_in_text("spotify_control est un outil utile")
    assert not agent.mentions_tool_call_in_text("execute_cli(command='ls')")  # non enregistré
    assert not agent.mentions_tool_call_in_text("")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── La consigne d'appel d'outil (mode local uniquement) ─────────────────────
#
# CES TESTS ONT DÉJÀ VERROUILLÉ LE BUG. Ils exigeaient la contre-instruction
# « N'écris JAMAIS l'appel en texte », c'est-à-dire exactement la variante E de
# l'ablation — mesurée sans AUCUN tool_call natif, à chaque tirage. Le modèle
# obéissait à l'interdiction, n'écrivait plus l'appel, n'en émettait toujours
# aucun nativement, et répondait « C'est fait. » sans rien exécuter. La suite
# restait verte pendant ce temps. La consigne est inversée : en local, écrire
# l'appel EST le mécanisme d'exécution.


def _system_prompt_for(provider: str) -> str:
    from jarvis.kernel.settings import Settings

    local_settings = Settings(_env_file=None)
    object.__setattr__(local_settings, "llm_provider", provider)

    registry = ToolRegistry()
    registry.register(_SpotifyTool())
    agent = Agent(
        settings=local_settings,  # type: ignore[arg-type]
        llm=_FakeLLM(""),  # type: ignore[arg-type]
        tool_registry=registry,
    )
    return agent._build_system()


def test_local_mode_is_told_to_write_the_call() -> None:
    """En local, la ligne écrite EST le transport d'exécution : le prompt doit
    la demander, avec le format exact que l'analyseur du gateway reconnaît."""
    prompt = _system_prompt_for("local")

    assert "ÉCRIS l'appel" in prompt
    assert 'spotify_control(action="pause")' in prompt


def test_local_mode_never_forbids_writing_the_call_again() -> None:
    """Garde-fou permanent contre la régression : plus jamais d'interdiction."""
    prompt = _system_prompt_for("local")

    assert "N'écris JAMAIS l'appel en texte" not in prompt
    assert "mécanisme natif OBLIGATOIRE" not in prompt


def test_local_mode_forbids_claiming_success_without_a_call() -> None:
    """« C'est fait. » sans appel est précisément ce qui a été observé."""
    prompt = _system_prompt_for("local")

    assert "c'est fait" in prompt.lower()


def test_cloud_mode_prompt_unchanged() -> None:
    """Claude appelle nativement malgré les exemples : ne pas alourdir son prompt."""
    prompt = _system_prompt_for("api")

    assert "ÉCRIS l'appel" not in prompt
    assert "N'écris JAMAIS l'appel en texte" not in prompt


# ── L'analyseur d'appels écrits en texte ────────────────────────────────────


def _agent_with(*tool_names: str) -> Agent:
    registry = ToolRegistry()
    for n in tool_names:
        tool = _SpotifyTool()
        tool.name = n  # type: ignore[misc]
        registry.register(tool)
    return Agent(settings=settings, llm=_FakeLLM(""), tool_registry=registry)  # type: ignore[arg-type]


def test_parser_extracts_name_and_args() -> None:
    agent = _agent_with("spotify_control")
    calls = agent.extract_text_tool_calls('[CF] spotify_control(action="pause")')

    assert len(calls) == 1
    _id, name, args = calls[0]
    assert name == "spotify_control"
    assert args == {"action": "pause"}


def test_parser_handles_nested_quotes_from_static_prompt() -> None:
    """Exemple littéral du prompt statique : guillemets simples DANS des doubles."""
    agent = _agent_with("execute_cli")
    calls = agent.extract_text_tool_calls('execute_cli(command="open -a \'Safari\'")')

    assert calls[0][2] == {"command": "open -a 'Safari'"}


def test_parser_handles_parens_inside_string() -> None:
    """Un `)` dans la chaîne ne doit pas fermer l'appel prématurément."""
    agent = _agent_with("execute_cli")
    calls = agent.extract_text_tool_calls(
        "execute_cli(command=\"yt-dlp -o '%(title)s.%(ext)s' URL\")"
    )

    assert calls[0][2]["command"] == "yt-dlp -o '%(title)s.%(ext)s' URL"


def test_parser_handles_multiple_args_and_types() -> None:
    agent = _agent_with("show_view")
    calls = agent.extract_text_tool_calls('show_view(action="show", view_id="clock")')

    assert calls[0][2] == {"action": "show", "view_id": "clock"}


def test_parser_extracts_several_calls_in_order() -> None:
    agent = _agent_with("spotify_control", "show_view")
    calls = agent.extract_text_tool_calls(
        'D\'abord show_view(action="show") puis spotify_control(action="pause")'
    )

    assert [n for _, n, _ in calls] == ["show_view", "spotify_control"]


def test_parser_ignores_unregistered_and_prose() -> None:
    agent = _agent_with("spotify_control")

    assert agent.extract_text_tool_calls("print(x) et execute_cli(command='ls')") == []
    assert agent.extract_text_tool_calls("spotify_control est un outil") == []
    assert agent.extract_text_tool_calls("") == []


def test_parser_ignores_unterminated_call() -> None:
    """Sortie tronquée : parenthèse jamais refermée -> on n'exécute rien."""
    agent = _agent_with("spotify_control")

    assert agent.extract_text_tool_calls('spotify_control(action="pau') == []


def test_local_prompt_routes_device_actions_to_cf_not_project() -> None:
    """Symptôme réel : « montre moi paris », « joue red house », « montre la
    météo » repartaient tous avec « C'est lancé, suis l'avancement dans le
    dashboard » — l'ack canonique de [BG:PROJECT]. La section [BG:PROJECT] du
    prompt statique pèse sept exemples sur dix ; un modèle 14B s'aligne sur le
    bloc le plus insistant. La consigne locale, placée après, rétablit [CF]."""
    prompt = _system_prompt_for("local")

    assert "Routage des actions" in prompt
    assert "Jamais `[BG:PROJECT]`" in prompt


def test_cloud_mode_has_no_routing_override() -> None:
    """Claude route correctement : ne pas alourdir son prompt."""
    prompt = _system_prompt_for("api")

    assert "Routage des actions" not in prompt

# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — Jarvis ne doit jamais affirmer une action qu'il n'a pas exécutée.

C'est le dernier maillon d'une série : la musique qui ne se met pas en pause, la
vue qui ne s'affiche pas, la mission « refusée par l'utilisateur ». À chaque
fois, une couche connaissait la vérité et ne la disait pas.

Preuve du 18/09 : « montre moi le cockpit » → « C'est lancé, le cockpit est
affiché. » et api.log ne contient AUCUN `Tool executed` sur ce tour. Le repli
existant était conditionné à la route [CF] ; or ce tour part en [I], donc il ne
se déclenchait pas. Le tag est une intention déclarée par le modèle, pas un
fait — on ne peut pas s'en servir comme garde-fou.

Le déclencheur est donc la PHRASE : si la réponse affirme qu'une action a eu
lieu et qu'aucun outil n'a tourné, on relance une fois en prompt minimal, puis
on remplace l'affirmation par un aveu.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.capabilities.tools.registry import ToolRegistry
from jarvis.engine.agent import Agent, claims_completion
from jarvis.engine.background.notifications import NotificationQueue
from jarvis.engine.gateway import Gateway
from jarvis.engine.session import SessionManager
from jarvis.kernel.settings import settings


class _ViewTool(Tool):
    name = "show_view"
    description = "Affiche une vue."
    input_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "view_id": {"type": "string"}},
        "required": ["action"],
    }

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, **kwargs: object) -> ToolResult:
        self.calls.append(dict(kwargs))
        return ToolResult(content="Vue affichée.")


class _LLM:
    """Premier jet figé ; `forced` est ce que rend la relance minimale."""

    def __init__(self, first: str, forced: str = "AUCUN", synth: str = "Voilà le cockpit.") -> None:
        self._first, self._forced, self._synth = first, forced, synth

    @property
    def supports_tools(self) -> bool:
        return True

    def stream_with_capture(
        self, messages: list[dict], system: str, tools: list[dict] | None = None
    ) -> tuple[AsyncIterator[str], object]:
        from jarvis.kernel.schemas import ToolCapture

        async def _gen() -> AsyncIterator[str]:
            for w in self._first.split(" "):
                yield w + " "

        return _gen(), ToolCapture()

    async def complete(self, *a: object, **k: object) -> AsyncIterator[str] | str:
        if k.get("stream") is False:
            return self._forced
        synth = self._synth

        async def _gen() -> AsyncIterator[str]:
            yield synth

        return _gen()


def _gateway(llm: _LLM) -> tuple[Gateway, _ViewTool]:
    tool = _ViewTool()
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(settings=settings, llm=llm, tool_registry=registry)  # type: ignore[arg-type]
    return (
        Gateway(
            session_manager=SessionManager(),
            agent=agent,
            notifications=NotificationQueue(),
            worker=None,  # type: ignore[arg-type]
        ),
        tool,
    )


async def _run(gw: Gateway, msg: str) -> str:
    _s, _r, resp = await gw.handle(msg, stream=True)
    assert not isinstance(resp, str)
    return "".join([c async for c in resp])


# ── Le détecteur ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "C'est lancé, le cockpit est affiché.",  # la phrase exacte observée
        "Voilà la météo.",
        "C'est fait.",
        "J'ai lancé la musique.",
        "C'est parti.",
    ],
)
def test_action_claims_are_detected(phrase: str) -> None:
    assert claims_completion(phrase)


@pytest.mark.parametrize(
    "phrase",
    [
        "Il est 22h53.",
        "Tu écoutes Nutshell par Alice In Chains.",
        "Je ne sais pas répondre à ça.",
        "La tour Eiffel mesure 330 mètres.",
    ],
)
def test_plain_answers_are_not_treated_as_claims(phrase: str) -> None:
    """Un faux positif remplacerait une réponse correcte par un aveu d'échec."""
    assert not claims_completion(phrase)


# ── Le garde-fou de bout en bout ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_without_any_tool_is_replaced_by_an_admission() -> None:
    """Le cas réel : [I] + affirmation + zéro outil exécuté."""
    gateway, tool = _gateway(_LLM("[I] C'est lancé, le cockpit est affiché.", forced="AUCUN"))

    out = await _run(gateway, "montre moi le cockpit")

    assert tool.calls == []
    assert "C'est lancé" not in out, "l'affirmation non vérifiée ne doit pas sortir"
    assert "pas réussi" in out


@pytest.mark.asyncio
async def test_the_retry_fires_outside_cf_and_rescues_the_turn() -> None:
    """Si la relance minimale trouve l'appel, l'action a lieu pour de vrai."""
    gateway, tool = _gateway(
        _LLM(
            "[I] C'est lancé, le cockpit est affiché.",
            forced='show_view(action="show", view_id="system-monitor")',
            synth="Voilà le cockpit.",
        )
    )

    out = await _run(gateway, "montre moi le cockpit")

    assert tool.calls == [{"action": "show", "view_id": "system-monitor"}]
    assert "Voilà le cockpit." in out


@pytest.mark.asyncio
async def test_a_normal_answer_is_left_alone() -> None:
    """Non-régression : pas d'affirmation d'action, pas de relance, pas de remplacement."""
    gateway, tool = _gateway(_LLM("[I] Il est 22h53."))

    out = await _run(gateway, "quelle heure il est")

    assert tool.calls == []
    assert "22h53" in out
    assert "pas réussi" not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

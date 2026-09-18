# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — « montre moi paris » doit faire bouger le globe.

Sortie réellement affichée le 17/09 :

    [Cf] [outil appelé] map_control({"action": "fly_to", "location": "paris", "zoom": 12})

Trois défauts enchaînés, chacun invisible seul :

1. `_flatten_content` (providers/llm/local.py) rendait les blocs tool_use sous
   la forme `[outil appelé] nom({json})` pour les réinjecter dans l'appel de
   synthèse. Le modèle lisait ce format comme sa propre parole et le recopiait
   en réponse — le texte ci-dessus est une imitation, pas un vrai appel.
2. `_parse_call_args` ne savait lire que `clef="valeur"`. Sur des arguments
   JSON entre parenthèses il rendait {} : l'appel était reconnu comme visant
   map_control, puis exécuté SANS action — donc en échec, globe immobile.
3. `_TAG_RE` était sensible à la casse : `[Cf]` ne matchait pas, la balise
   restait affichée et la route retombait sur le défaut.

Le front, lui, était correct de bout en bout : home.js écoute `map_fly_to`,
active la vue et relaie vers `view.command('fly_to')`, qui appelle `map.flyTo`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.capabilities.tools.registry import ToolRegistry
from jarvis.engine.agent import Agent
from jarvis.engine.router import RouteEnum, SpeedRouter
from jarvis.kernel.settings import settings

_REEL = '[Cf] [outil appelé] map_control({"action": "fly_to", "location": "paris", "zoom": 12})'


class _MapTool(Tool):
    name = "map_control"
    description = "Contrôle la carte/globe."
    input_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "location": {"type": "string"}},
        "required": ["action"],
    }

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, **kwargs: object) -> ToolResult:
        self.calls.append(dict(kwargs))
        return ToolResult(content="Navigation effectuée.")


def _agent() -> Agent:
    registry = ToolRegistry()
    registry.register(_MapTool())
    return Agent(settings=settings, llm=MagicMock(), tool_registry=registry)


# ── 2. arguments JSON entre parenthèses ─────────────────────────────────────


def test_json_arguments_inside_parentheses_are_parsed() -> None:
    """Le cœur du bug : l'outil partait sans action et échouait en silence."""
    calls = _agent().extract_text_tool_calls(_REEL)

    assert len(calls) == 1
    _cid, name, args = calls[0]
    assert name == "map_control"
    assert args == {"action": "fly_to", "location": "paris", "zoom": 12}


def test_classic_key_value_arguments_still_work() -> None:
    """Non-régression sur la notation d'origine."""
    calls = _agent().extract_text_tool_calls('map_control(action="zoom_in")')

    assert calls[0][2] == {"action": "zoom_in"}


def test_braces_that_are_not_json_fall_back_to_key_value() -> None:
    calls = _agent().extract_text_tool_calls('map_control({pas du json}, action="zoom_in")')

    assert calls[0][2] == {"action": "zoom_in"}


# ── 3. tag de routing insensible à la casse ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["[CF]", "[Cf]", "[cf]"])
async def test_route_tag_is_case_insensitive(tag: str) -> None:
    async def _stream():  # noqa: ANN202
        yield f"{tag} Voilà Paris."

    route, text_stream = await SpeedRouter.extract_route(_stream())
    text = "".join([c async for c in text_stream])

    assert route is RouteEnum.CONFIRM_FIRE
    assert "[" not in text, f"la balise {tag} est restée visible : {text!r}"


# ── 1. le format réinjecté au modèle ne doit plus être un appel ─────────────


def test_flatten_no_longer_renders_a_re_emittable_call() -> None:
    """Si ce rendu ressemble à un appel, le modèle le recopie — et l'analyseur
    le relit comme un appel sans arguments."""
    from jarvis.providers.llm.local import _flatten_content

    out = _flatten_content(
        [
            {"type": "text", "text": "ok"},
            {
                "type": "tool_use",
                "id": "t1",
                "name": "map_control",
                "input": {"action": "fly_to", "location": "paris"},
            },
        ]
    )

    assert "map_control(" not in out, "format ré-émettable : le modèle va le copier"
    assert "[outil appelé]" not in out
    assert "map_control" in out, "le modèle doit savoir quel outil a tourné"
    assert "paris" in out, "et avec quels arguments, pour formuler sa réponse"


def test_flattened_tool_use_is_not_parsed_back_as_a_call() -> None:
    """Le test qui ferme la boucle : ce qu'on montre au modèle, relu par
    l'analyseur, ne doit produire AUCUN appel."""
    from jarvis.providers.llm.local import _flatten_content

    rendered = _flatten_content(
        [{"type": "tool_use", "id": "t1", "name": "map_control", "input": {"action": "fly_to"}}]
    )

    assert _agent().extract_text_tool_calls(rendered) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

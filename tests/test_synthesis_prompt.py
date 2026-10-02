# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""La synthese repond ; elle n'appelle plus d'outil.

Incident du 01/10 : « montre mes courriels non lus ». list_emails avait tourne,
puis la synthese a repondu `list_emails(action="list", unread_only=True)`. Son
prompt systeme etait celui du premier appel : catalogue d'outils et consigne
« ÉCRIS l'appel », qui exige d'ecrire un appel avant d'affirmer une action.
"""

from __future__ import annotations

from jarvis.capabilities.tools.registry import ToolRegistry
from jarvis.engine.agent import Agent
from jarvis.kernel.settings import Settings
from tests.test_gateway_tool_fallback import _FakeLLM, _SpotifyTool


def _agent() -> Agent:
    local = Settings(_env_file=None)
    object.__setattr__(local, "llm_provider", "local")
    registry = ToolRegistry()
    registry.register(_SpotifyTool())
    return Agent(
        settings=local,  # type: ignore[arg-type]
        llm=_FakeLLM(""),  # type: ignore[arg-type]
        tool_registry=registry,
    )


def test_la_synthese_ne_recoit_pas_la_consigne_d_ecrire_un_appel() -> None:
    synthese = _agent()._build_system(for_synthesis=True)
    assert "ÉCRIS l'appel" not in synthese
    assert "## Outils disponibles" not in synthese
    assert "les outils ont DÉJÀ été exécutés" in synthese
    # La consigne de reponse est la DERNIERE section : la plus proche de la reponse.
    assert synthese.rstrip().endswith("dis-le simplement.")
    assert "SANS markdown" in synthese and "2 ou 3 phrases" in synthese


def test_le_premier_appel_garde_la_consigne_d_appel() -> None:
    routage = _agent()._build_system()
    assert "ÉCRIS l'appel" in routage
    assert "les outils ont DÉJÀ été exécutés" not in routage

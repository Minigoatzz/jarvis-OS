# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Aucune requete ne peut occuper le modele indefiniment.

Le 30/09, deux syntheses du chat ont genere sans fin : 57 s puis 4 min 59 s
d'apres le server.log d'Ollama, jusqu'a ce que le client coupe. Le modele est
partage : pendant ce temps, le bouton d'envoi restait gele et le moteur
proactif attendait derriere. Chaque requete porte donc un plafond de jetons.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.providers.llm import local
from jarvis.providers.llm.local import OllamaProvider


def test_chaque_reponse_de_chat_est_plafonnee() -> None:
    for stream in (True, False):
        options = OllamaProvider()._payload([], "s", stream=stream)["options"]
        assert options["num_predict"] == local._MAX_REPLY_TOKENS
    assert 0 < local._MAX_REPLY_TOKENS <= 4096


@pytest.mark.asyncio
async def test_chaque_tour_d_outil_de_mission_est_plafonne() -> None:
    reponse = MagicMock()
    reponse.raise_for_status = MagicMock()
    reponse.json = MagicMock(return_value={"message": {"content": "fait"}})
    client = MagicMock()
    client.post = AsyncMock(return_value=reponse)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    async def executor(name: str, args: dict) -> str:
        return ""

    with patch("jarvis.providers.llm.local.httpx.AsyncClient", return_value=ctx):
        await OllamaProvider().tool_loop(
            messages=[{"role": "user", "content": "x"}],
            system="s",
            tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
            tool_executor=executor,
        )
    payload = client.post.call_args.kwargs["json"]
    assert payload["options"]["num_predict"] == local._MAX_TOOL_TURN_TOKENS

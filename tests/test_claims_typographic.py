# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Une affirmation d'action sans outil reste un mensonge, quelle que soit l'apostrophe.

07/10 : « archive celui de Jean Coutu » → « C’est fait, le courriel de Jean Coutu
est archivé. » ; « annule » → « C’est annulé. ». AUCUN outil n'avait tourne
(api.log). Le detecteur cherchait « c'est » avec l'apostrophe droite ; le modele
ecrit l'apostrophe typographique, et « archive », « annule » n'etaient pas listes.
"""

from __future__ import annotations

import pytest

from jarvis.engine.agent import claims_completion


@pytest.mark.parametrize(
    "phrase",
    [
        "C’est fait, le courriel de Jean Coutu est archivé.",  # exacte, 07/10
        "C’est annulé.",  # exacte, 07/10
        "C’est en pause.",
        "J’ai archivé les courriels.",
        "Le courriel est bien supprimé.",
        "Les fichiers sont créés.",
        "Je l’ai mis en pause.",
        "J’ai ouvert Spotify.",
        "[Courriel de Jean Coutu] a été archivé. Tu veux lire le prochain ?",  # 08/10
        '[Email "Test Jarvis"] a été supprimé.',  # 08/10
        "Ils ont été déplacés.",
    ],
)
def test_les_affirmations_typographiques_sont_detectees(phrase: str) -> None:
    assert claims_completion(phrase)


@pytest.mark.parametrize(
    "phrase",
    [
        "C’est compliqué.",
        "Aucun événement prévu cette semaine.",
        "C’est un courriel de Jean Coutu sur une livraison rapide.",
        "Il est jouable de partir tôt.",
        "Le magasin est ouvert jusqu’à 21h.",
        "L’été dernier était chaud.",
    ],
)
def test_une_reponse_ordinaire_n_est_pas_une_affirmation(phrase: str) -> None:
    """Un faux positif remplacerait une vraie reponse par un aveu d'echec."""
    assert not claims_completion(phrase)


def test_le_journal_dit_ce_qu_un_outil_a_recu_et_rendu() -> None:
    import asyncio

    from loguru import logger

    from jarvis.capabilities.tools.base import Tool, ToolResult
    from jarvis.capabilities.tools.registry import ToolRegistry

    class _Echo(Tool):
        name = "echo"
        description = "d"
        input_schema = {"type": "object"}  # noqa: RUF012

        async def execute(self, **kwargs: object) -> ToolResult:
            return ToolResult(content="ligne 1\nligne 2 " + "x" * 400)

    lignes: list[str] = []
    sink = logger.add(lambda m: lignes.append(m.record["message"]), level="INFO")
    try:
        registry = ToolRegistry()
        registry.register(_Echo())
        asyncio.run(registry.call("echo", {"query": "from:jeancoutu"}))
    finally:
        logger.remove(sink)
    trace = next(line for line in lignes if line.startswith("Tool echo |"))
    assert "from:jeancoutu" in trace and "ligne 1 ligne 2" in trace
    assert "car.]" in trace, "un resultat long est borne, pas recopie en entier"

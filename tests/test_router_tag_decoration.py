# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Extraction du tag de routage : enrobage et espacement.

Le 28/09, « `[I]` Fait. » s'affichait « `` Fait. » : le modele avait entoure
son tag d'accents graves. Chaque cas est teste sous PLUSIEURS decoupages du
flux — un seul decoupage masquait deux defauts, dont un anterieur a ce
correctif (double espace selon l'endroit de la coupure).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from jarvis.engine.router import SpeedRouter

_DECOUPAGES = (1, 2, 3, 4, 5, 7, 100)


def _extraire(texte: str, taille: int) -> tuple[str, str]:
    async def _flux() -> AsyncIterator[str]:
        for i in range(0, len(texte), taille):
            yield texte[i : i + taille]

    async def _go() -> tuple[str, str]:
        route, reste = await SpeedRouter.extract_route(_flux())
        return route.value, "".join([c async for c in reste])

    return asyncio.run(_go())


@pytest.mark.parametrize("taille", _DECOUPAGES)
@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("`[I]` Fait. Le fichier a été créé.", ("I", "Fait. Le fichier a été créé.")),
        ("``[I]`` Fait.", ("I", "Fait.")),
        ("**[CF]** Je lance.", ("CF", "Je lance.")),
        ("```\n[I] Fait.", ("I", "Fait.")),
        ("`[I]`", ("I", "")),
    ],
)
def test_un_tag_enrobe_ne_laisse_aucune_trace(
    brut: str, attendu: tuple[str, str], taille: int
) -> None:
    assert _extraire(brut, taille) == attendu


@pytest.mark.parametrize("taille", _DECOUPAGES)
def test_seul_l_enrobage_symetrique_est_retire(taille: int) -> None:
    """Un vrai gras qui suit le tag doit survivre."""
    assert _extraire("`[I]` **Attention** au bruit.", taille) == (
        "I",
        "**Attention** au bruit.",
    )


@pytest.mark.parametrize("taille", _DECOUPAGES)
@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("[I] Fait.", ("I", "Fait.")),
        ("[I]  Fait.", ("I", "Fait.")),
        ("D'accord. [CF] Je lance.", ("CF", "D'accord. Je lance.")),
        ("D'accord.[CF] Je lance.", ("CF", "D'accord. Je lance.")),
        ("[BG:PROJECT] C'est lancé.", ("BG:PROJECT", "C'est lancé.")),
        ("Bonjour, comment ça va ?", ("I", "Bonjour, comment ça va ?")),
    ],
)
def test_l_espacement_ne_depend_pas_du_decoupage(
    brut: str, attendu: tuple[str, str], taille: int
) -> None:
    assert _extraire(brut, taille) == attendu

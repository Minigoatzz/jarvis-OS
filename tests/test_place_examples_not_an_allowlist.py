# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — les exemples de lieux ne doivent pas se lire comme une liste blanche.

Observation de l'utilisateur, 18/09 : « la tour eiffel marche souvent mais
pour ex moscou ou des trucs de même souvent ça fonctionne moins ».

Les lieux qui marchaient étaient EXACTEMENT ceux nommés en exemple dans le
prompt — Lyon, Tokyo, Paris, tour Eiffel, mont Fuji. Moscou, Shanghai et
Whistler n'apparaissaient nulle part dans les prompts. La corrélation ne suit
PAS les tables de géocodage : `moscou` est dans `CITY_COORDS`, `tour eiffel`
n'y est pas (elle vit dans la table de show_view). Ce sont les exemples du
prompt qui prédisaient le succès, pas la capacité réelle de l'outil.

Le mécanisme est la mise en forme : dans `show_view`, la ligne
`✓ "Lyon", "Tokyo", "tour Eiffel", "mont Fuji"` suivait immédiatement
`❌ NE JAMAIS utiliser pour planètes, étoiles…`. Un couple ✓/✗ ne se lit pas
comme « voici des exemples de catégorie » mais comme « voici les valeurs
permises, voici les interdites ». Moscou n'était sur aucune des deux listes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jarvis.capabilities.tools.map_control import CITY_COORDS
from jarvis.capabilities.tools.show_view import ShowViewTool

_ROOT = Path(__file__).resolve().parents[1]
_STATIC_PROMPT = _ROOT / "prompts" / "system_static.md"

# Lieux réels que l'utilisateur a vus échouer. Aucun n'est un exemple du prompt.
_ABSENTS = ("moscou", "shanghai", "whistler")


def _static_prompt_text() -> str:
    """Le fichier est en CRLF sur disque — on normalise avant toute recherche."""
    return _STATIC_PROMPT.read_text(encoding="utf-8").replace("\r\n", "\n")


def _map_control_section() -> str:
    text = _static_prompt_text()
    start = text.index("### Contrôle de la carte / globe (map_control)")
    nxt = text.find("\n### ", start + 10)
    return text[start:] if nxt == -1 else text[start:nxt]


# ── Le prompt statique ──────────────────────────────────────────────────────


def test_trigger_uses_a_placeholder_not_only_named_cities() -> None:
    """« Montre-moi <lieu> » enseigne le motif ; « Montre-moi Lyon » enseigne Lyon."""
    section = _map_control_section()

    assert "<lieu>" in section, (
        "le déclencheur ne cite que des villes nommées — le modèle apprend "
        "la liste au lieu de la catégorie."
    )


def test_static_prompt_denies_the_allowlist_reading() -> None:
    section = _map_control_section().lower()

    assert "aucune liste de lieux autorisés" in section


# ── La description de l'outil ───────────────────────────────────────────────


def test_show_view_does_not_present_a_bare_checkmark_list_of_cities() -> None:
    """Une ligne `✓ "A", "B", "C"` en face d'un `❌` se lit comme une liste blanche."""
    description = ShowViewTool.description

    bare_list = re.search(r"✓\s*(\"[^\"]+\"\s*,\s*){2,}", description)
    assert bare_list is None, (
        f"liste blanche apparente : {bare_list.group(0)!r} — "
        "nomme la catégorie, pas une énumération de lieux."
    )


def test_show_view_says_the_examples_are_not_exhaustive() -> None:
    description = ShowViewTool.description.lower()

    assert "pas une liste de lieux autorisés" in description
    assert "tout lieu réel" in description


# ── L'invariant de fond ─────────────────────────────────────────────────────


@pytest.mark.parametrize("place", _ABSENTS)
def test_failing_places_are_now_covered_by_the_prompts(place: str) -> None:
    """Chacun doit être soit nommé, soit couvert par une formule générale."""
    blob = (_static_prompt_text() + ShowViewTool.description).lower()

    assert place in blob or "tout lieu réel" in blob, (
        f"« {place} » n'est ni cité ni couvert par une règle générale."
    )


def test_the_geocoder_was_never_the_problem() -> None:
    """Preuve que la corrélation observée ne venait pas des tables de coordonnées.

    Si le géocodage expliquait la panne, `moscou` aurait dû manquer et
    `tour eiffel` être présent. C'est l'inverse.
    """
    assert "moscou" in CITY_COORDS, "moscou EST géocodable — la panne était ailleurs"
    assert "tour eiffel" not in CITY_COORDS, (
        "tour eiffel n'est pas dans CITY_COORDS et marchait pourtant : "
        "la réussite suivait les exemples du prompt, pas le géocodeur."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

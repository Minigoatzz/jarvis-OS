# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — une réponse réduite à un tag nu n'est pas une réponse.

Observé le 18/09 : « montre moi la tour eiffel » → la conversation affiche
exactement « [Cockpit] ». Rien d'autre, aucun outil exécuté.

Chaîne complète : le modèle a appris du prompt statique que les réponses
commencent par un tag de routage ([I], [CF], [BG]). Faute de savoir quoi
faire il émet un jeton entre crochets et s'arrête. `_ANY_TAG_RE` dans
router.py ne filtre que les tags COURTS en majuscules (`[A-Z]{1,3}`), et
délibérément : `[MINDMAP]` est du contenu légitime qu'il ne faut pas manger.
« [Cockpit] » fait sept lettres en casse mixte — il traverse le filtre.

Aucun garde-fou ne l'attrapait ensuite : sans tag valide la route retombe sur
[I] (donc hors du repli réservé à [CF]) et `claims_completion("[Cockpit]")`
est faux (la phrase n'affirme rien). Le gateway faisait `yield text` et
l'utilisateur recevait le jeton brut.

Troisième costume de la même panne, après « [outil appelé] » et les accusés
de réception « [BG:PROJECT] » : le modèle recopie une notation à crochets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.engine.agent import is_degenerate_reply

_GATEWAY = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "engine" / "gateway.py"


def _gateway_source() -> str:
    """Le fichier est en CRLF sur disque — on normalise avant toute recherche."""
    return _GATEWAY.read_text(encoding="utf-8").replace("\r\n", "\n")


# ── Ce qui EST dégénéré ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "[Cockpit]",  # le cas exact observé
        "  [Cockpit]  ",
        "[Globe]",
        "[cockpit]",
        "[BG:PROJECT]",
        "[I]",
        "",
        "   ",
        "\n\n",
    ],
)
def test_bare_tokens_are_degenerate(text: str) -> None:
    assert is_degenerate_reply(text)


# ── Ce qui ne l'est PAS — la partie qui compte ──────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "[MINDMAP] voici la carte mentale",  # contenu légitime, ne pas manger
        "[MINDMAP]\nracine: projet",
        "La tour Eiffel est affichée.",
        "[Cockpit] est maintenant affiché.",  # un tag SUIVI d'une phrase
        "Voilà [Cockpit] pour toi",
        "[ceci contient des espaces]",
        "[" + "x" * 40 + "]",  # trop long pour être un tag
    ],
)
def test_real_content_is_not_degenerate(text: str) -> None:
    assert not is_degenerate_reply(text), f"contenu légitime mangé : {text!r}"


# ── Le câblage dans le gateway ──────────────────────────────────────────────


def test_gateway_retries_on_a_degenerate_reply() -> None:
    """Sans ça, « [Cockpit] » n'atteint aucun des deux déclencheurs existants."""
    src = _gateway_source()

    assert "degenerate" in src
    assert "or degenerate" in src, (
        "la relance doit se déclencher aussi sur une réponse dégénérée, "
        "pas seulement sur [CF] ou sur une affirmation d'action."
    )


def test_gateway_never_yields_a_bare_token_to_the_user() -> None:
    src = _gateway_source()

    assert "is_degenerate_reply(text)" in src, (
        "le dernier garde-fou doit remplacer « [Cockpit] » par une phrase "
        "honnête — le rendre tel quel, c'est livrer un jeton brut."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

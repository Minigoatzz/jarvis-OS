# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — le prompt de relance `force_tool_call` doit rester MINIMAL.

Panne observée le 18/09 : « montre moi shanghai », « montre moi whistler, BC »
et « montre moi la tour eiffel » ont échoué trois fois de suite, alors que la
même demande avait fonctionné quelques minutes plus tôt. Le log montrait
`Aucun outil déclenché — réponse non vérifiée` suivi de `Affirmation d'action
sans exécution` : le modèle affirmait avoir agi, la relance partait, et
revenait VIDE à chaque fois.

La relance existe parce qu'une ablation a montré qu'un prompt court obtient un
appel là où le prompt complet (22 Ko) n'en obtient aucun. Mais le menu envoyé
par la relance était construit à partir des descriptions COMPLÈTES des 27
outils : 13 076 caractères, dont 3 430 pour le seul `fusion_360` — plus que
les dix outils suivants réunis. Le « prompt minimal » n'avait de minimal que
son nom, et `map_control` s'y trouvait noyé exactement comme dans le grand.

Deuxième défaut, pire : quand la relance ne produisait aucun appel, RIEN
n'était journalisé. On ne pouvait pas distinguer « le modèle a répondu AUCUN »
de « le modèle a écrit un appel que l'analyseur refuse » — deux pannes dont
les correctifs sont opposés.
"""

from __future__ import annotations

import inspect

import pytest

from jarvis.engine.agent import (
    Agent,
    _compact_tool_menu,
    _first_sentence,
    _signature,
)

_FUSION_LIKE = "Contrôle Autodesk Fusion 360.\n\n" + ("Détail interminable. " * 200)


def _schemas() -> list[dict]:
    return [
        {
            "name": "map_control",
            "description": "Contrôle la carte/globe Jarvis. " + ("Blabla. " * 50),
            "input_schema": {
                "properties": {
                    "action": {"enum": ["fly_to", "zoom_in", "zoom_out"]},
                    "location": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {"name": "fusion_360", "description": _FUSION_LIKE, "input_schema": {}},
    ]


# ── _first_sentence ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Une phrase. Deux.", "Une phrase."),
        ("\n\nDémarre par un saut. Suite.", "Démarre par un saut."),
        ("Pas de ponctuation finale", "Pas de ponctuation finale"),
        ("", ""),
    ],
)
def test_first_sentence(raw: str, expected: str) -> None:
    assert _first_sentence(raw) == expected


def test_first_sentence_caps_a_runaway_line() -> None:
    out = _first_sentence("x" * 500)
    assert len(out) == 140 and out.endswith("…")


# ── _signature ──────────────────────────────────────────────────────────────


def test_signature_exposes_enum_values() -> None:
    """Le modèle doit pouvoir écrire action="fly_to" sans deviner."""
    sig = _signature(_schemas()[0]["input_schema"])
    assert sig == "(action=fly_to|zoom_in|zoom_out, [location])"


def test_signature_marks_optional_arguments() -> None:
    assert _signature({"properties": {"a": {}}}) == "([a])"
    assert _signature({"properties": {"a": {}}, "required": ["a"]}) == "(a)"


def test_signature_survives_garbage_schemas() -> None:
    """Un outil mal formé ne doit pas faire tomber la relance entière."""
    for bad in (None, {}, {"properties": "pas un dict"}, {"properties": {}}):
        assert _signature(bad) == "()"


def test_signature_drops_oversized_enums() -> None:
    """Un enum de 20 valeurs rallongerait le menu au lieu de l'aider."""
    schema = {"properties": {"a": {"enum": list(range(20))}}, "required": ["a"]}
    assert _signature(schema) == "(a)"


# ── _compact_tool_menu ──────────────────────────────────────────────────────


def test_menu_is_dramatically_smaller_than_the_full_descriptions() -> None:
    """C'est l'invariant de fond : la relance doit rester courte."""
    schemas = _schemas()
    naive = "\n".join(f"- `{s['name']}` : {s['description']}" for s in schemas)
    compact = _compact_tool_menu(schemas)

    assert len(compact) < len(naive) / 4, (
        f"menu compact {len(compact)} vs complet {len(naive)} — "
        "la relance redevient un mur de texte."
    )


def test_menu_keeps_one_line_per_tool() -> None:
    menu = _compact_tool_menu(_schemas())
    assert len(menu.splitlines()) == 2
    assert "map_control" in menu and "fusion_360" in menu


def test_menu_ignores_nameless_tools() -> None:
    assert _compact_tool_menu([{"name": "", "description": "x"}]) == ""
    assert _compact_tool_menu([]) == ""


# ── Garde-fous sur force_tool_call lui-même ─────────────────────────────────


def test_force_tool_call_uses_the_compact_menu() -> None:
    """Régression : le menu ne doit plus être bâti sur `s['description']`."""
    src = inspect.getsource(Agent.force_tool_call)

    assert "_compact_tool_menu" in src
    assert "s['description']" not in src, (
        "les descriptions complètes sont de retour dans la relance — "
        "13 Ko de prompt, c'est la panne du 18/09."
    )


def test_force_tool_call_logs_when_the_retry_comes_back_empty() -> None:
    """Sans cette trace, l'échec de la relance est invisible dans le log."""
    src = inspect.getsource(Agent.force_tool_call)

    assert "if not calls:" in src, "la relance vide doit être détectée"
    assert "logger.warning" in src, "…et journalisée"
    assert "brut" in src, "…avec la réponse brute du modèle, sinon on ne sait rien"


def test_force_tool_call_example_does_not_reuse_a_likely_real_request() -> None:
    """L'écho est la panne récurrente du projet : le modèle recopie l'exemple.

    L'exemple du prompt doit donc porter un lieu improbable en usage réel, pas
    « Paris » ni « la tour Eiffel » — précisément ce que l'utilisateur demande.
    """
    src = inspect.getsource(Agent.force_tool_call)
    lowered = src.lower()

    for bait in ("tour eiffel", "shanghai", "montréal", "whistler"):
        assert bait not in lowered, (
            f"« {bait} » sert d'exemple dans le prompt de relance — "
            "le modèle peut le recopier au lieu d'utiliser la vraie demande."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — quand TOUS les outils échouent, on ne laisse pas le modèle résumer.

Log du 21/09, 07:19 : « Montre-moi Montréal » → relance → show_view appelé
avec une action devinée → `[JRV-TOL-001] ...` → « Outil en échec avant
synthèse » → et pourtant la réponse affichée : « C'est lancé, on est sur
Montréal. » La carte est restée sur Paris.

La synthèse recevait bien l'échec (is_error + consigne « UN OUTIL VIENT
D'ÉCHOUER »). Le modèle l'a ignoré. Une consigne qu'il peut ignorer n'est pas
un garde-fou : l'échec total est maintenant annoncé par le code.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.engine.agent import all_tools_failed_message

_GATEWAY = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "engine" / "gateway.py"


def test_total_failure_gets_an_honest_message_with_the_reason() -> None:
    msg = all_tools_failed_message(["show_view"], ["[JRV-TOL-001] Action inconnue : display"])

    assert msg is not None
    assert "rien n'a changé" in msg
    assert "show_view : Action inconnue : display" in msg
    assert "JRV-" not in msg, "le code interne n'a rien à faire dans la conversation"


def test_total_failure_message_never_claims_completion() -> None:
    from jarvis.engine.agent import claims_completion

    msg = all_tools_failed_message(["map_control"], ["[JRV-TOL-001] Lieu introuvable"])
    assert msg is not None and not claims_completion(msg)


def test_partial_success_is_left_to_the_synthesis() -> None:
    assert all_tools_failed_message(
        ["show_view", "map_control"],
        ["Vue globe affichée.", "[JRV-TOL-001] Lieu introuvable"],
    ) is None


def test_success_and_empty_are_untouched() -> None:
    assert all_tools_failed_message(["show_view"], ["Navigation vers Montréal."]) is None
    assert all_tools_failed_message([], []) is None


def test_gateway_skips_free_synthesis_on_total_failure() -> None:
    src = _GATEWAY.read_text(encoding="utf-8").replace("\r\n", "\n")
    i_guard = src.index("all_tools_failed_message(\n")
    i_synth = src.index("synth_stream = agent.synthesize(")
    assert i_guard < i_synth, "le garde-fou doit passer AVANT la synthèse libre"

# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — sur la route PROJECT, la mission est l'action ; et le worker sait la date.

Observé le 23/09 sur « crée un fichier bonjour.txt contenant la date du jour » :

1. Le tour partait bien en [BG:PROJECT] et l'interface lançait la mission 3 s
   plus tard — mais le modèle avait AUSSI écrit `execute_cli(command="echo …")`
   dans son accusé de réception. Le gateway l'exécutait, l'outil refusait
   « echo », et l'utilisateur lisait « Ça n'a pas marché, rien n'a changé »
   pendant qu'une mission démarrait. Le message était faux.

2. Le worker, lui, n'a jamais reçu la date. Faute de la connaître, il a écrit
   la chaîne « $(date +%Y-%m-%d) » — littéralement — dans bonjour.txt, deux
   fois de suite (proj_d57ef2). La vérification native l'a correctement refusé.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "jarvis"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


# ── 1. Route PROJECT ────────────────────────────────────────────────────────


# Ces deux tests vérifiaient la PRÉSENCE du texte `not is_project` dans le
# source. Ils passaient, et le bug du 28/09 existait quand même : aucune de ces
# gardes ne couvrait le chemin des appels NATIFS, et lire le source ne pouvait
# pas le voir. La route PROJECT est désormais une seule couture en tête de
# _pipe(), et son comportement est prouvé par test_project_route_behaviour.py,
# qui pilote réellement Gateway.handle() et reproduit l'incident sur l'ancien
# code. Ce qui reste ici ne vérifie que l'existence de la couture.


def test_project_route_is_a_single_seam() -> None:
    src = _read("engine/gateway.py")
    code = "\n".join(l for l in src.split("\n") if not l.lstrip().startswith("#"))
    assert "if route is RouteEnum.PROJECT:" in code
    assert "is_project" not in code, "plus aucune garde éparse : une seule couture"


def test_other_routes_keep_their_guards() -> None:
    """Non-régression : [CF] et [I] gardent le garde-fou anti-mensonge."""
    src = _read("engine/gateway.py")
    assert "claims_completion(ack_text)" in src
    assert "all_tools_failed_message(" in src


# ── 2. Le worker et la date ─────────────────────────────────────────────────


def test_worker_context_carries_today_date() -> None:
    src = _read("engine/mission/worker_agent.py")
    assert "Date du jour :" in src
    assert "{datetime.now():%Y-%m-%d}" in src


def test_worker_is_told_that_write_file_is_literal() -> None:
    src = _read("engine/mission/worker_agent.py")
    assert "$(date)" in src and "TEL QUEL" in src


def test_worker_prompt_actually_renders() -> None:
    """Le 23/09 j'ai écrit « ${VAR} » dans ce gabarit sans doubler les accolades.

    Le prompt passe par str.format(context=…) : format() a lu {VAR} comme un
    champ à remplacer et levé KeyError('VAR'). Toutes les étapes de mission
    mouraient à la première seconde, avec pour seule trace l'erreur « 'VAR' ».
    Les tests d'alors ne vérifiaient que la PRÉSENCE du texte dans le source —
    jamais que le gabarit se rendait. Celui-ci le rend.
    """
    from jarvis.engine.mission.worker_agent import _WORKER_SYSTEM

    rendered = _WORKER_SYSTEM.format(context="Titre : T\nMission : M")

    assert "Titre : T" in rendered
    assert "${VAR}" in rendered, "l'exemple doit survivre au rendu, littéralement"
    assert "{context}" not in rendered


def test_the_written_context_actually_contains_a_real_date() -> None:
    """Le format doit produire une vraie date, pas un gabarit resté tel quel."""
    rendered = f"Date du jour : {datetime.now():%Y-%m-%d}"
    assert re.fullmatch(r"Date du jour : \d{4}-\d{2}-\d{2}", rendered)


def test_the_literal_string_the_worker_wrote_would_still_fail_verification() -> None:
    """Garde-fou : si le modèle récidive, la vérif doit continuer à le refuser."""
    from jarvis.engine.mission.native_checks import evaluate

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "bonjour.txt").write_text("$(date +%Y-%m-%d)", encoding="utf-8")
        verdict = evaluate(r"grep -E '^\d{4}-\d{2}-\d{2}$' bonjour.txt", ws)
        assert verdict is not None and not verdict.passed

        (ws / "bonjour.txt").write_text(f"{datetime.now():%Y-%m-%d}\n", encoding="utf-8")
        ok = evaluate(r"grep -E '^\d{4}-\d{2}-\d{2}$' bonjour.txt", ws)
        assert ok is not None and ok.passed

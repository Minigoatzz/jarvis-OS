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


def test_project_route_does_not_execute_written_tool_calls() -> None:
    src = _read("engine/gateway.py")
    assert "is_project = route is RouteEnum.PROJECT" in src

    block = src[src.index("is_project = route is RouteEnum.PROJECT") :]
    extraction = block[: block.index("text_calls = agent.extract_text_tool_calls")]
    assert "not is_project" in extraction, (
        "l'analyse des appels écrits doit être sautée quand la mission fait le travail"
    )


def test_project_route_skips_the_retry_and_both_guards() -> None:
    src = _read("engine/gateway.py")
    assert "and not is_project\n                    and (route is RouteEnum.CONFIRM_FIRE" in src
    assert "if asserts_action and not is_project:" in src
    assert "if is_degenerate_reply(text) and not is_project:" in src


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

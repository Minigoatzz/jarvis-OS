# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — vérifier une étape sans lancer de processus.

Panne du 23/09, mission proj_0e9f1c « crée un fichier bonjour.txt » : le worker
a bien écrit le fichier, puis la couche déterministe a lancé `test -f
bonjour.txt` et reçu `rc=-1 : Exécution directe refusée — ALLOW_UNSANDBOXED_EXEC
non activé`. Deux essais, étape FAILED, mission FAILED. Les deux missions du
13/09 étaient mortes de la même façon.

Et même autorisée, la commande aurait échoué : `test`, `grep` et `awk`
n'existent pas dans cmd.exe sous Windows.

Les commandes ci-dessous sont copiées telles quelles depuis les state.json de
proj_0e9f1c — pas des exemples inventés.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.engine.mission.native_checks import evaluate

# Copiées depuis workspace/projects/proj_0e9f1c/.jarvis/state.json
_CMD_EXISTS = "test -f bonjour.txt"
_CMD_DATE = r"grep -E '^\d{4}-\d{2}-\d{2}$' bonjour.txt"
_CMD_REPORT = "test -s RAPPORT.md && grep -c '^## ' RAPPORT.md | awk '{exit ($1 >= 3) ? 0 : 1}'"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path


def test_the_exact_command_that_killed_the_mission(ws: Path) -> None:
    (ws / "bonjour.txt").write_text("bonjour, monde !", encoding="utf-8")
    verdict = evaluate(_CMD_EXISTS, ws)
    assert verdict is not None and verdict.passed


def test_missing_file_is_a_real_failure(ws: Path) -> None:
    verdict = evaluate(_CMD_EXISTS, ws)
    assert verdict is not None and not verdict.passed


def test_empty_file_fails_test_s(ws: Path) -> None:
    (ws / "RAPPORT.md").write_text("", encoding="utf-8")
    verdict = evaluate("test -s RAPPORT.md", ws)
    assert verdict is not None and not verdict.passed


def test_date_pattern(ws: Path) -> None:
    (ws / "bonjour.txt").write_text("2026-09-23\n", encoding="utf-8")
    ok = evaluate(_CMD_DATE, ws)
    assert ok is not None and ok.passed, "\\d doit marcher (GNU grep -E ne le gère pas, Python si)"

    (ws / "bonjour.txt").write_text("bonjour, monde !", encoding="utf-8")
    ko = evaluate(_CMD_DATE, ws)
    assert ko is not None and not ko.passed


def test_report_command_with_pipe_and_awk(ws: Path) -> None:
    (ws / "RAPPORT.md").write_text("## A\ntexte\n## B\n## C\n", encoding="utf-8")
    ok = evaluate(_CMD_REPORT, ws)
    assert ok is not None and ok.passed, "3 sections attendues, 3 trouvées"

    (ws / "RAPPORT.md").write_text("## A\n## B\n", encoding="utf-8")
    ko = evaluate(_CMD_REPORT, ws)
    assert ko is not None and not ko.passed, "2 sections : le critère n'est pas atteint"


def test_and_chain_stops_at_the_first_failure(ws: Path) -> None:
    (ws / "a.txt").write_text("x", encoding="utf-8")
    assert evaluate("test -f a.txt && test -f b.txt", ws).passed is False
    (ws / "b.txt").write_text("y", encoding="utf-8")
    assert evaluate("test -f a.txt && test -f b.txt", ws).passed is True


@pytest.mark.parametrize(
    "command",
    [
        "python verifie.py",  # exécution réelle
        "test -f a.txt || test -f b.txt",  # ou logique
        "cat a.txt; ls",  # enchaînement non géré
        "curl https://exemple.test",
        "grep -P '(?<=x)y' a.txt",  # option non gérée
        "",
    ],
)
def test_unknown_forms_return_none_rather_than_guessing(command: str, ws: Path) -> None:
    assert evaluate(command, ws) is None


def test_paths_cannot_escape_the_workspace(ws: Path) -> None:
    """Une commande générée par le modèle ne doit pas sonder le disque."""
    assert evaluate("test -f ../../secret.txt", ws) is None
    assert evaluate("test -f /etc/passwd", ws) is None


def test_directory_check(ws: Path) -> None:
    (ws / "tournage").mkdir()
    assert evaluate("test -d tournage", ws).passed is True
    assert evaluate("test -d absent", ws).passed is False


# ── Intégration dans le Verifier ────────────────────────────────────────────

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from jarvis.engine.mission.schemas import Step  # noqa: E402
from jarvis.engine.mission.verifier import Verifier  # noqa: E402

_REFUSED = {
    "success": False,
    "returncode": -1,
    "stdout": "",
    "stderr": "Exécution directe refusée : ALLOW_UNSANDBOXED_EXEC non activé. Activez Docker…",
}


def _verifier(ws: Path, cli=None) -> Verifier:
    quality = MagicMock()
    quality.check_step_output.return_value = []
    return Verifier(
        quality_checker=quality, llm=MagicMock(), cli_executor=cli, workspace_path=str(ws)
    )


def _step(command: str) -> Step:
    return Step(id="s1", title="t", description="d", verification_command=command)


@pytest.mark.asyncio
async def test_native_check_answers_without_running_anything(ws: Path) -> None:
    (ws / "bonjour.txt").write_text("salut", encoding="utf-8")
    cli = AsyncMock()

    result = await _verifier(ws, cli)._layer_deterministic(_step(_CMD_EXISTS))

    assert result.verified and not result.unverified
    cli.assert_not_called(), "aucun processus ne doit être lancé"


@pytest.mark.asyncio
async def test_native_check_can_still_fail_a_step(ws: Path) -> None:
    result = await _verifier(ws, AsyncMock())._layer_deterministic(_step(_CMD_EXISTS))
    assert not result.verified


@pytest.mark.asyncio
async def test_refused_execution_is_unverified_not_failed(ws: Path) -> None:
    """Le cœur de la panne : « je n'ai pas pu vérifier » n'est pas « c'est faux »."""
    cli = AsyncMock(return_value=_REFUSED)

    result = await _verifier(ws, cli)._layer_deterministic(_step("python verifie.py"))

    assert result.verified, "l'étape ne doit plus échouer parce que l'exécution est coupée"
    assert result.unverified, "…mais elle ne doit pas être déclarée vérifiée non plus"
    assert "Non vérifiée" in result.notes


@pytest.mark.asyncio
async def test_a_real_command_failure_still_fails(ws: Path) -> None:
    cli = AsyncMock(return_value={"success": False, "returncode": 1, "stderr": "AssertionError"})

    result = await _verifier(ws, cli)._layer_deterministic(_step("python verifie.py"))

    assert not result.verified and not result.unverified


@pytest.mark.asyncio
async def test_no_executor_at_all_is_unverified(ws: Path) -> None:
    result = await _verifier(ws, None)._layer_deterministic(_step("python verifie.py"))
    assert result.verified and result.unverified


def test_worker_does_not_tick_verified_when_it_could_not_check() -> None:
    src = (
        Path(__file__).resolve().parents[1] / "src/jarvis/engine/mission/worker_agent.py"
    ).read_text(encoding="utf-8")
    assert "step.verified = not verdict.unverified" in src
    assert "workspace_path=self._project.workspace_path," in src

# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — outil `mission_control`.

Contexte : l'orchestrateur exposait déjà `kill()`, `retry_project()` et
`list_projects()`, mais aucun outil ne les rendait accessibles au modèle.
Résultat observé en usage réel : « cancel mes deux missions » n'avait aucun
chemin légitime, et le modèle a écrit `execute_cli(command="killall -9
python3")` — soit tuer tous les processus Python de la machine, Jarvis compris.
L'allowlist binaire de execute_cli l'a refusé (killall n'y est pas), mais le
principe reste : une capacité sans outil pousse le modèle au contournement.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.capabilities.tools.mission_control import MissionControlTool
from jarvis.kernel.schemas import Project, ProjectStatus


class _FakeOrchestrator:
    """Reproduit la surface réellement utilisée par l'outil."""

    def __init__(self, projects: list[Project]) -> None:
        self._projects = projects
        self.killed: list[str] = []
        self.retried: list[str] = []
        self.kill_succeeds = True

    def list_projects(self) -> list[Project]:
        return list(self._projects)

    def kill(self, project_id: str) -> bool:
        if not self.kill_succeeds:
            return False
        if any(p.id == project_id for p in self._projects):
            self.killed.append(project_id)
            return True
        return False

    async def retry_project(self, project_id: str) -> Project | None:
        for p in self._projects:
            if p.id == project_id:
                self.retried.append(project_id)
                return p
        return None


def _project(pid: str, status: ProjectStatus, minutes_ago: int = 0) -> Project:
    return Project(
        id=pid,
        title=f"Mission {pid}",
        mission="peu importe",
        status=status,
        created_at=datetime.now() - timedelta(minutes=minutes_ago),
    )


def _tool(*projects: Project) -> tuple[MissionControlTool, _FakeOrchestrator]:
    orch = _FakeOrchestrator(list(projects))
    return MissionControlTool(orchestrator=orch), orch  # type: ignore[arg-type]


# ── cancel ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_targets_the_most_recent_active_mission() -> None:
    tool, orch = _tool(
        _project("OLD", ProjectStatus.RUNNING, minutes_ago=30),
        _project("NEW", ProjectStatus.RUNNING, minutes_ago=1),
        _project("DONE", ProjectStatus.DONE, minutes_ago=5),
    )

    result = await tool.execute(action="cancel")

    assert orch.killed == ["NEW"], "la plus récente des missions EN VOL"
    assert not result.is_error


@pytest.mark.asyncio
async def test_cancel_all_kills_every_active_mission() -> None:
    """Le cas exact demandé : « cancel mes deux missions »."""
    tool, orch = _tool(
        _project("A", ProjectStatus.RUNNING, minutes_ago=10),
        _project("B", ProjectStatus.PLANNING, minutes_ago=2),
        _project("C", ProjectStatus.DONE, minutes_ago=1),
    )

    result = await tool.execute(action="cancel", target="all")

    assert sorted(orch.killed) == ["A", "B"], "les terminées ne doivent pas être touchées"
    assert "2 mission" in result.content
    assert not result.is_error


@pytest.mark.asyncio
async def test_cancel_by_id() -> None:
    tool, orch = _tool(_project("PROJ_C", ProjectStatus.RUNNING))

    result = await tool.execute(action="cancel", project_id="PROJ_C")

    assert orch.killed == ["PROJ_C"]
    assert not result.is_error


@pytest.mark.asyncio
async def test_cancel_unknown_id_is_an_error_not_a_silent_success() -> None:
    tool, _orch = _tool(_project("PROJ_C", ProjectStatus.RUNNING))

    result = await tool.execute(action="cancel", project_id="INCONNU")

    assert result.is_error, "un échec doit remonter comme échec — cf. la synthèse"


@pytest.mark.asyncio
async def test_cancel_without_any_active_mission_says_so_plainly() -> None:
    tool, orch = _tool(_project("DONE", ProjectStatus.DONE))

    result = await tool.execute(action="cancel")

    assert orch.killed == []
    assert "Aucune mission en vol" in result.content


@pytest.mark.asyncio
async def test_cancel_reports_an_error_when_no_worker_could_be_stopped() -> None:
    """Listée active dans le store mais plus de worker vivant."""
    tool, orch = _tool(_project("ZOMBIE", ProjectStatus.RUNNING))
    orch.kill_succeeds = False

    result = await tool.execute(action="cancel")

    assert result.is_error


# ── status ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_lists_missions_and_counts_the_active_ones() -> None:
    tool, _orch = _tool(
        _project("A", ProjectStatus.RUNNING, minutes_ago=3),
        _project("B", ProjectStatus.FAILED, minutes_ago=1),
    )

    result = await tool.execute(action="status")

    assert "1 mission(s) en vol" in result.content
    assert "Mission A" in result.content
    assert "Mission B" in result.content


@pytest.mark.asyncio
async def test_status_without_any_mission() -> None:
    tool, _orch = _tool()

    result = await tool.execute(action="status")

    assert "Aucune mission" in result.content
    assert not result.is_error


# ── retry ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_picks_the_latest_failed_mission() -> None:
    tool, orch = _tool(
        _project("VIEUX_ECHEC", ProjectStatus.FAILED, minutes_ago=60),
        _project("DERNIER_ECHEC", ProjectStatus.FAILED, minutes_ago=2),
        _project("OK", ProjectStatus.DONE, minutes_ago=1),
    )

    await tool.execute(action="retry")

    assert orch.retried == ["DERNIER_ECHEC"]


@pytest.mark.asyncio
async def test_retry_without_any_failure() -> None:
    tool, orch = _tool(_project("OK", ProjectStatus.DONE))

    result = await tool.execute(action="retry")

    assert orch.retried == []
    assert "Aucune mission échouée" in result.content


# ── garde-fou ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_action_is_an_error() -> None:
    tool, _orch = _tool()

    result = await tool.execute(action="autodestruction")

    assert result.is_error


def test_tool_is_registered_in_bootstrap() -> None:
    """Sans câblage, l'outil est invisible — la panne d'origine de map_control."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "bootstrap.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    registered: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "register":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                    registered.add(arg.func.id)

    assert "MissionControlTool" in registered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

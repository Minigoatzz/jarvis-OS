# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — réclamation d'étape périmée, et « succès » avec une étape sautée.

proj_eb0459, 24/09. La mission s'affiche « terminée » à 3/4 : l'étape 2 est
restée en attente. Journal :

    warni Étape déjà réclamée par un autre worker : Écrire la date du jour…
    info  ✓ Projet terminé avec succès

Deux défauts enchaînés :
  1. `release_step_claim` n'était appelé que par la pause budgétaire. Le worker
     mort du premier essai gardait sa réclamation ; au Retry, le nouveau worker
     trouvait l'étape « déjà réclamée » et la sautait.
  2. Une étape sautée ne lève aucun échec : la boucle allait au bout et le
     projet se déclarait DONE avec une étape jamais exécutée.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.engine.mission import orchestrator as orch_mod
from jarvis.engine.mission.orchestrator import ProjectOrchestrator
from jarvis.engine.mission.schemas import Project, ProjectStatus, Step, StepStatus


class _Store:
    def __init__(self, projects: list[Project]) -> None:
        self.projects = {p.id: p for p in projects}
        self.released: list[tuple[str, str]] = []
        self.saved: list[str] = []

    def list_projects(self) -> list[Project]:
        return list(self.projects.values())

    def load_project(self, pid: str) -> Project | None:
        return self.projects.get(pid)

    def save_project(self, project: Project) -> None:
        self.saved.append(project.id)

    def release_step_claim(self, project_id: str, step_id: str) -> None:
        self.released.append((project_id, step_id))


def _project(pid: str, status: ProjectStatus, steps: list[StepStatus]) -> Project:
    return Project(
        id=pid,
        title="t",
        mission="m",
        status=status,
        steps=[Step(id=f"s{i}", title=f"étape {i}", description="d", status=s)
               for i, s in enumerate(steps)],
    )


def _orch(store) -> ProjectOrchestrator:
    return ProjectOrchestrator(
        broadcast_event=MagicMock(),
        store=store,  # type: ignore[arg-type]
        manager=MagicMock(),
        worker_llm=MagicMock(),
    )


# ── 1. La réclamation doit être libérée ─────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_releases_the_claim_of_every_reset_step() -> None:
    """Sans ça, le Retry saute l'étape que le worker mort réclamait encore."""
    p = _project("p", ProjectStatus.FAILED, [StepStatus.DONE, StepStatus.FAILED])
    store = _Store([p])
    orch = _orch(store)
    store.released.clear()

    worker = MagicMock()
    worker.run = AsyncMock(return_value=None)
    with patch.object(orch_mod, "WorkerAgent", return_value=worker):
        await orch.retry_project("p")
        await asyncio.sleep(0)

    assert ("p", "s1") in store.released, "l'étape remise en attente doit être libérée"
    assert ("p", "s0") not in store.released, "une étape déjà faite n'est pas touchée"


def test_startup_releases_claims_of_interrupted_steps() -> None:
    p = _project("p", ProjectStatus.RUNNING, [StepStatus.RUNNING])
    store = _Store([p])

    _orch(store)

    assert store.released == [("p", "s0")]


# ── 2. Pas de « succès » avec une étape jamais exécutée ─────────────────────


def test_worker_refuses_to_sign_off_an_incomplete_mission() -> None:
    src = (
        Path(__file__).resolve().parents[1] / "src/jarvis/engine/mission/worker_agent.py"
    ).read_text(encoding="utf-8").replace("\r\n", "\n")

    branch = src[src.index("unfinished = ["): src.index("project.status = ProjectStatus.DONE")]
    assert "StepStatus.PENDING" in branch and "StepStatus.WAITING_APPROVAL" in branch
    assert "ProjectStatus.FAILED" in branch
    assert "Mission incomplète" in branch


@pytest.mark.asyncio
async def test_incomplete_mission_is_marked_failed() -> None:
    """Comportement, pas source : une étape en attente ⇒ mission en échec."""
    from jarvis.engine.mission.worker_agent import WorkerAgent

    project = _project("p", ProjectStatus.RUNNING, [StepStatus.DONE, StepStatus.PENDING])
    project.workspace_path = "/tmp/jarvis-test-ws"
    worker = WorkerAgent(
        project=project,
        store=MagicMock(),
        broadcast_event=MagicMock(),
        approval_callback=AsyncMock(return_value=True),
        llm=MagicMock(),
    )
    worker._log = AsyncMock()  # type: ignore[method-assign]
    worker._push_update = MagicMock()  # type: ignore[method-assign]
    worker._execute_step = AsyncMock()  # type: ignore[method-assign]
    worker._setup_environment = AsyncMock()  # type: ignore[method-assign]
    worker._verifier = None

    await worker.run()

    assert project.status is ProjectStatus.FAILED, "3/4 ne doit pas s'appeler « terminée »"
    messages = " ".join(str(c) for c in worker._log.call_args_list)
    assert "incomplète" in messages


# ── 3. La fenêtre d'approbation sur toutes les pages ────────────────────────

_STATIC = Path(__file__).resolve().parents[1] / "src/jarvis/interfaces/ui/static"


@pytest.mark.parametrize("page", ["home.html", "dashboard.html", "capabilities.html", "settings.html"])
def test_every_page_loads_the_approval_window(page: str) -> None:
    """Une permission demandée pendant qu'on est sur le dashboard n'était jamais
    affichée — et expirait en refus au bout de 2 minutes, sans rien montrer."""
    html = (_STATIC / page).read_text(encoding="utf-8")
    assert "/home_overlays.js" in html, f"{page} n'affiche pas les demandes d'approbation"


@pytest.mark.parametrize("page", ["home.html", "dashboard.html", "capabilities.html", "settings.html"])
def test_every_page_gets_cache_busting_for_it(page: str) -> None:
    ui = (Path(__file__).resolve().parents[1] / "src/jarvis/interfaces/api/ui.py").read_text(
        encoding="utf-8"
    )
    assert ui.count('("/home_overlays.js"') == 4


def test_overlays_opens_its_own_socket_only_when_needed() -> None:
    js = (_STATIC / "home_overlays.js").read_text(encoding="utf-8")
    assert "JARVIS_WS_RELAY" in js and "new WebSocket" in js
    assert "approvalKey" in js, "dédoublonnage requis : deux sources possibles"

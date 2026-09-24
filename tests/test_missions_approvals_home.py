# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — missions interrompues, et ce que la page reçoit pour approuver.

Constat du 21/09 dans workspace/projects :
  - proj_224dba « running » depuis le 13/09, étape « waiting_approval » : son
    worker était mort avec le process. Comptée active, impossible à annuler.
  - aucune approbation n'avait jamais pu être donnée : home.html (servie sur
    « / ») n'écoutait pas `approval_request`. La page en a maintenant une
    (home_overlays.js) ; elle affiche le titre de la mission et le vrai temps
    restant — ces deux champs doivent donc exister dans le message.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.engine import approval_checker as ac
from jarvis.engine.mission import orchestrator as orch_mod
from jarvis.engine.mission.orchestrator import ProjectOrchestrator
from jarvis.engine.mission.schemas import Project, ProjectStatus, Step, StepStatus


class _Store:
    def __init__(self, projects: list[Project]) -> None:
        self.projects = {p.id: p for p in projects}
        self.saved: list[str] = []
        self.released: list[tuple[str, str]] = []

    def list_projects(self) -> list[Project]:
        return list(self.projects.values())

    def load_project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def save_project(self, project: Project) -> None:
        self.saved.append(project.id)

    def release_step_claim(self, project_id: str, step_id: str) -> None:
        # Le vrai ProjectStore en a une : sans elle ici, le faux ne représentait
        # plus le contrat (ajout du 24/09, cf. test_mission_claims_*).
        self.released.append((project_id, step_id))


def _orch(store, broadcast=None) -> ProjectOrchestrator:
    return ProjectOrchestrator(
        broadcast_event=broadcast or MagicMock(),
        store=store,  # type: ignore[arg-type]
        manager=MagicMock(),
        worker_llm=MagicMock(),
    )


def _project(pid: str, status: ProjectStatus, steps: list[StepStatus]) -> Project:
    return Project(
        id=pid,
        title=f"Titre {pid}",
        mission="m",
        status=status,
        steps=[Step(id=f"s{i}", title="t", description="d", status=s) for i, s in enumerate(steps)],
    )


# ── Missions orphelines au démarrage ────────────────────────────────────────


def test_orphaned_running_mission_is_marked_failed_at_startup() -> None:
    """Le cas exact de proj_224dba."""
    zombie = _project(
        "proj_224dba",
        ProjectStatus.RUNNING,
        [StepStatus.SKIPPED, StepStatus.WAITING_APPROVAL, StepStatus.PENDING],
    )
    store = _Store([zombie])

    _orch(store)

    assert zombie.status is ProjectStatus.FAILED
    assert zombie.steps[1].status is StepStatus.FAILED
    assert "Jarvis s'est arrêté" in (zombie.steps[1].error or "")
    assert zombie.steps[0].status is StepStatus.SKIPPED, "le passé n'est pas réécrit"
    assert zombie.steps[2].status is StepStatus.PENDING
    assert store.saved == ["proj_224dba"]


def test_interrupted_mission_stays_retryable() -> None:
    """retry_project remet les étapes `failed` en attente : c'est ce qu'on vise."""
    p = _project("p", ProjectStatus.PLANNING, [StepStatus.RUNNING])
    _orch(_Store([p]))
    assert p.steps[0].status is StepStatus.FAILED


def test_finished_missions_are_left_alone() -> None:
    done = _project("d", ProjectStatus.DONE, [StepStatus.DONE])
    failed = _project("f", ProjectStatus.FAILED, [StepStatus.FAILED])
    paused = _project("z", ProjectStatus.PAUSED, [StepStatus.PENDING])
    store = _Store([done, failed, paused])

    _orch(store)

    assert store.saved == []
    assert (done.status, failed.status, paused.status) == (
        ProjectStatus.DONE,
        ProjectStatus.FAILED,
        ProjectStatus.PAUSED,
    )


def test_unreadable_store_does_not_prevent_startup() -> None:
    store = MagicMock()
    store.list_projects.side_effect = OSError("disque illisible")
    _orch(store)  # ne doit pas lever


# ── Ce que la page reçoit ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mission_approval_request_carries_title_and_real_timeout() -> None:
    sent: list[dict] = []
    p = _project("proj_x", ProjectStatus.DONE, [])
    orch = _orch(_Store([p]), broadcast=sent.append)

    with patch.object(orch_mod, "_APPROVAL_TIMEOUT_S", 0.05):
        decision = await orch._request_approval("proj_x", "s3", "Écrire RAPPORT.md")

    assert decision is None, "sans réponse : None, pas un refus"
    msg = sent[-1]
    assert msg["type"] == "approval_request"
    assert (msg["project_id"], msg["step_id"]) == ("proj_x", "s3")
    assert msg["project_title"] == "Titre proj_x"
    assert msg["timeout_s"] == 0.05, "la page doit afficher le délai RÉELLEMENT attendu"


@pytest.mark.asyncio
async def test_mission_approval_survives_an_unknown_project() -> None:
    sent: list[dict] = []
    orch = _orch(_Store([]), broadcast=sent.append)
    with patch.object(orch_mod, "_APPROVAL_TIMEOUT_S", 0.05):
        await orch._request_approval("inconnu", "s1", "d")
    assert sent[-1]["project_title"] == "inconnu"


@pytest.mark.asyncio
async def test_tool_permission_request_carries_the_real_timeout() -> None:
    sent: list[dict] = []
    checker = ac.ApprovalChecker(broadcast_event=sent.append)
    with patch.object(ac, "_APPROVAL_TIMEOUT_S", 0.05), patch.object(
        ac.approval_config, "file_write", ac.ApprovalMode.ASK
    ):
        allowed = await checker.check("file_write", "notes.txt", "act-1")

    assert allowed is False, "sans réponse, une permission reste refusée"
    assert sent[-1]["action_id"] == "act-1"
    assert sent[-1]["timeout_s"] == 0.05


@pytest.mark.asyncio
async def test_answered_mission_approval_is_resolved() -> None:
    orch = _orch(_Store([]))
    task = asyncio.create_task(orch._request_approval("p", "s", "d"))
    await asyncio.sleep(0)
    assert orch.resolve_approval("p", "s", True) is True
    assert await task is True


# ── Le câblage de la page ───────────────────────────────────────────────────

from pathlib import Path  # noqa: E402

_STATIC = Path(__file__).resolve().parents[1] / "src/jarvis/interfaces/ui/static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_home_page_loads_the_overlays_before_home_js() -> None:
    html = _read("home.html")
    assert html.index('src="/home_overlays.js"') < html.index('src="/home.js"')


def test_home_js_relays_approval_requests() -> None:
    assert 'data.type === "approval_request"' in _read("home.js")
    assert "JarvisOverlays?.handleApprovalRequest(data)" in _read("home.js")


def test_overlays_file_is_cache_busted() -> None:
    """Sans ça, une correction future du fichier resterait masquée par le cache."""
    ui = (Path(__file__).resolve().parents[1] / "src/jarvis/interfaces/api/ui.py").read_text(encoding="utf-8")
    assert '("/home_overlays.js", "src/jarvis/interfaces/ui/static/home_overlays.js")' in ui


def test_escape_hook_respects_an_already_handled_key() -> None:
    """Trouvé par le test navigateur : Échap fermait la palette ET la vue."""
    shared = _read("_shared.js")
    assert "!hadOverlay && !e.defaultPrevented" in shared


@pytest.mark.asyncio
async def test_retry_also_restarts_a_step_stuck_waiting_for_approval() -> None:
    """proj_ca3b34 : étape figée en waiting_approval depuis le 13/09 — la
    demande n'avait jamais pu s'afficher, et `retry` ne la rejouait pas."""
    p = _project("p", ProjectStatus.FAILED, [StepStatus.WAITING_APPROVAL, StepStatus.FAILED])
    orch = _orch(_Store([p]))
    orch._workers.clear()

    worker = MagicMock()
    worker.run = AsyncMock(return_value=None)
    with patch.object(orch_mod, "WorkerAgent", return_value=worker):
        await orch.retry_project("p")
        await asyncio.sleep(0)  # laisse la tâche du worker se terminer proprement

    assert [s.status for s in p.steps] == [StepStatus.PENDING, StepStatus.PENDING]

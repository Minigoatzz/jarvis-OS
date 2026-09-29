# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Cycle de vie d'une mission : qui annonce quoi, et quand.

Le 28/09, la mission proj_6cf963 a reussi (3/3 etapes verifiees) et
l'utilisateur a conclu qu'aucune mission n'existait : rien ne le lui disait.
Ces tests verrouillent chaque annonce, et les trois facons dont une mission
pouvait finir sans que personne ne le sache.
"""

from __future__ import annotations

import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.engine.mission import announcements as A
from jarvis.engine.mission.orchestrator import ProjectOrchestrator
from jarvis.engine.mission.worker_agent import WorkerAgent
from jarvis.kernel.schemas import Project, ProjectStatus, Step, StepStatus


def _project(**kw: object) -> Project:
    defaults: dict = {
        "id": "proj_6cf963",
        "title": "Créer fichier bonjour.txt",
        "mission": "m",
        "steps": [Step("s1", "Écrire la date", "d", success_criterion="x")],
        "workspace_path": tempfile.mkdtemp(),
    }
    defaults.update(kw)
    return Project(**defaults)  # type: ignore[arg-type]


def _worker(project: Project, events: list) -> WorkerAgent:
    w = WorkerAgent(
        project=project,
        store=MagicMock(),
        broadcast_event=events.append,
        approval_callback=AsyncMock(),  # type: ignore[arg-type]
        llm=MagicMock(),
    )
    w._log = AsyncMock()  # type: ignore[method-assign]
    return w


def _chat(events: list) -> list[str]:
    return [e["text"] for e in events if e.get("type") == "message"]


# ── 1. Les messages eux-memes ───────────────────────────────────────────────


def test_chaque_annonce_tient_dans_le_canal_d_accueil() -> None:
    """showChannel() tronque a 160 : une annonce plus longue perd sa fin."""
    p = _project(status=ProjectStatus.FAILED)
    p.steps[0].status = StepStatus.FAILED
    p.steps[0].error = "x" * 500
    for texte in (A.launch_ack(), A.created(p), A.finished(p), A.plan_invalid("y" * 500)):
        assert len(texte) <= A.CHANNEL_LIMIT


def test_un_echec_nomme_toujours_l_etape_et_la_cause() -> None:
    p = _project(status=ProjectStatus.FAILED)
    p.steps[0].status = StepStatus.FAILED
    p.steps[0].error = "Commande introuvable : 'touch'"
    assert "Écrire la date" in A.finished(p)
    assert "touch" in A.finished(p)


def test_un_echec_sans_etape_fautive_garde_sa_cause() -> None:
    p = _project(status=ProjectStatus.FAILED)
    assert "délai dépassé" in A.finished(p, reason="délai dépassé")


def test_l_annonce_de_lancement_ne_pretend_rien_de_fait() -> None:
    """« Fait. » avait ete affiche alors que la mission n'existait pas encore."""
    assert "Fait" not in A.launch_ack()
    assert "Mission lancée" in A.launch_ack()


# ── 2. Le worker annonce la fin, quel que soit le statut ────────────────────


@pytest.mark.parametrize(
    "status", [ProjectStatus.DONE, ProjectStatus.FAILED, ProjectStatus.KILLED]
)
def test_toute_fin_de_mission_est_annoncee(status: ProjectStatus) -> None:
    """project_done n'etait emis qu'au SUCCES : un echec restait muet."""
    events: list = []
    w = _worker(_project(status=status), events)
    w._announce_finished()

    fin = [e for e in events if e.get("type") == "project_finished"]
    assert fin and fin[0]["status"] == str(status)
    assert _chat(events), "la conversation doit l'apprendre aussi"


def test_une_pause_budget_n_est_pas_une_fin() -> None:
    events: list = []
    _worker(_project(status=ProjectStatus.PAUSED), events)._announce_finished()
    assert events == []


def test_un_echec_de_mise_en_place_termine_la_mission() -> None:
    """_setup_environment etait appele AVANT le try : s'il levait, la mission
    restait RUNNING pour toujours, sans passer par finally."""
    events: list = []
    p = _project()
    w = _worker(p, events)
    w._setup_environment = AsyncMock(side_effect=NotImplementedError)  # type: ignore[method-assign]

    asyncio.run(w.run())

    assert p.status is ProjectStatus.FAILED
    assert any("NotImplementedError" in t for t in _chat(events)), (
        "la cause doit etre nommee, meme quand l'exception n'a pas de message"
    )


# ── 3. Le delai d'une mission ───────────────────────────────────────────────


def test_une_mission_qui_depasse_son_delai_est_close_et_annoncee() -> None:
    """wait_for ANNULE run() : CancelledError n'est pas une Exception, et la
    mission etait sauvegardee en RUNNING, pour toujours."""
    events: list = []
    p = _project(timeout_minutes=1)
    orch = ProjectOrchestrator(
        broadcast_event=events.append,
        store=MagicMock(),  # type: ignore[arg-type]
        manager=MagicMock(),
        worker_llm=MagicMock(),
    )
    w = _worker(p, events)
    w._setup_environment = AsyncMock()  # type: ignore[method-assign]

    async def _sans_fin(step: Step) -> None:
        step.status = StepStatus.RUNNING
        await asyncio.sleep(3600)

    w._execute_step = _sans_fin  # type: ignore[method-assign]
    orch._workers[p.id] = w
    p.timeout_minutes = 0.1 / 60  # type: ignore[assignment]

    async def _go() -> None:
        await orch._start_worker(w, p, name="t")

    asyncio.run(_go())

    assert p.status is ProjectStatus.FAILED
    assert p.steps[0].status is StepStatus.FAILED
    assert "délai" in (p.steps[0].error or "")
    assert any(e.get("type") == "project_finished" for e in events)
    assert p.id not in orch._workers, "un worker fini ne doit plus etre tuable"


def test_un_seul_point_de_lancement_des_workers() -> None:
    import pathlib

    src = pathlib.Path("src/jarvis/engine/mission/orchestrator.py").read_text(encoding="utf-8")
    assert src.count("wait_for(worker.run()") == 1, "creation, retry et reprise : une seule copie"


# ── 4. Lancer une mission depuis une interface ──────────────────────────────


def _orchestrator(events: list, manager: MagicMock) -> ProjectOrchestrator:
    return ProjectOrchestrator(
        broadcast_event=events.append,
        store=MagicMock(),  # type: ignore[arg-type]
        manager=manager,
        worker_llm=MagicMock(),
    )


def test_un_planificateur_en_panne_est_annonce() -> None:
    """chat.py et proactive.py faisaient un create_task nu : la panne mourait
    en « Task exception was never retrieved »."""
    events: list = []
    manager = MagicMock()
    manager.create_project = AsyncMock(side_effect=ConnectionError("Ollama injoignable"))
    orch = _orchestrator(events, manager)

    async def _go() -> None:
        await orch.launch_in_background("crée un fichier", origin="test")

    asyncio.run(_go())
    assert any("Ollama injoignable" in t for t in _chat(events))


def test_un_plan_inexploitable_est_annonce_une_seule_fois() -> None:
    """Plus de rejet pour critere manquant : seul un plan vide ou illisible echoue,
    au normaliseur, et launch_in_background l'annonce — une fois."""
    from jarvis.engine.mission.plan_normalizer import PlanError

    events: list = []
    manager = MagicMock()
    manager.create_project = AsyncMock(side_effect=PlanError("le plan ne contient aucune étape"))
    orch = _orchestrator(events, manager)

    async def _go() -> None:
        await orch.launch_in_background("crée un fichier", origin="test")

    asyncio.run(_go())
    annonces = _chat(events)
    assert len(annonces) == 1 and "aucune étape" in annonces[0]


def test_un_critere_manquant_ne_rejette_plus_le_plan() -> None:
    import pathlib

    src = pathlib.Path("src/jarvis/engine/mission/orchestrator.py").read_text(encoding="utf-8")
    assert "validate_step" not in src


@pytest.mark.parametrize(
    "module", ["websocket", "chat", "proactive"]
)
def test_les_interfaces_passent_par_le_point_d_entree_unique(module: str) -> None:
    import pathlib

    src = pathlib.Path(f"src/jarvis/interfaces/api/{module}.py").read_text(encoding="utf-8")
    assert "launch_in_background(" in src
    assert "create_and_run(" not in src


def test_la_page_d_accueil_affiche_les_notifications() -> None:
    """Aucune page n'affichait le type `notification` : tout partait dans le vide."""
    import pathlib

    js = pathlib.Path("src/jarvis/interfaces/ui/static/home.js").read_text(encoding="utf-8")
    assert 'data.type === "notification"' in js

# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Un « non » de l'utilisateur arrete la mission — une question par action, jamais deux.

Incident du 29/09 (proj_53189a) : « cree trois notes, puis supprime notes2.txt ».
Quatre fenetres pour UNE suppression :
1. l'etape entiere (drapeau `requires_approval` du plan) ;
2. l'outil delete_file (le vrai controle) — refuse ;
3. et 4. l'etape « Corriger d'apres la recette », qui reposait la question.
Et l'etape refusee se cochait ✓, parce que le refus revenait au modele comme un
simple message d'outil.

Ces tests pilotent le VRAI worker (vrai store, vraie gouvernance) avec un LLM
factice qui insiste : il rappelle delete_file trois fois.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

import jarvis.engine.mission.project_store as project_store
from jarvis.engine.mission.project_store import ProjectStore
from jarvis.engine.mission.worker_agent import WorkerAgent
from jarvis.kernel.schemas import Project, ProjectStatus, Step, StepStatus


class _LLM:
    """`tool_loop` rejoue une liste d'actions par etape ; `complete` sert la recette."""

    def __init__(self, actions: list[list[tuple[str, dict]]], verdicts: list[dict]) -> None:
        self._actions = list(actions)
        self._verdicts = list(verdicts)
        self.recettes = 0
        self.reponses_outils: list[str] = []

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        context: str = "",
    ) -> str:
        for name, args in self._actions.pop(0) if self._actions else []:
            self.reponses_outils.append(await tool_executor(name, args))
        return "fait"

    async def complete(self, messages: list[dict], system: str, **_: object) -> str:
        self.recettes += 1
        return json.dumps(self._verdicts.pop(0))


class _Approbations:
    """Callback d'approbation qui compte les questions et rend `decision`."""

    def __init__(self, decision: bool | None) -> None:
        self.decision = decision
        self.questions: list[str] = []

    async def __call__(self, project_id: str, step_id: str, description: str) -> bool | None:
        self.questions.append(description)
        return self.decision


_CREER = [
    ("write_file", {"path": "notes1.txt", "content": "a"}),
    ("write_file", {"path": "notes2.txt", "content": "b"}),
    ("write_file", {"path": "notes3.txt", "content": "c"}),
]
_SUPPRIMER_EN_INSISTANT = [("delete_file", {"path": "notes2.txt"})] * 3


@pytest.fixture
def lancer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Lance un worker isole : jamais le vrai dossier de missions."""
    monkeypatch.setattr(project_store, "WORKSPACE_DIR", tmp_path / "claims")

    def _lancer(
        titres: list[str], llm: _LLM, approbations: _Approbations, *, drapeau: bool = False
    ) -> tuple[Project, list[dict]]:
        ws = tmp_path / uuid.uuid4().hex[:6]
        (ws / ".jarvis").mkdir(parents=True)
        projet = Project(
            id="t" + uuid.uuid4().hex[:6],
            title="Notes",
            mission="crée trois fichiers de notes, puis supprime notes2.txt",
            workspace_path=str(ws),
            steps=[
                Step(f"step_{i:03d}", t, "d", requires_approval=drapeau)
                for i, t in enumerate(titres, 1)
            ],
        )
        evenements: list[dict] = []
        worker = WorkerAgent(
            project=projet,
            store=ProjectStore(),
            broadcast_event=evenements.append,
            approval_callback=approbations,
            llm=llm,  # type: ignore[arg-type]
        )
        asyncio.run(worker.run())
        return projet, evenements

    return _lancer


def _annonce(evenements: list[dict]) -> str:
    return [e["text"] for e in evenements if e.get("type") == "message"][-1]


def test_un_non_arrete_la_mission_sans_reposer_la_question(lancer) -> None:  # noqa: ANN001
    llm = _LLM([_CREER, _SUPPRIMER_EN_INSISTANT], [])
    approbations = _Approbations(False)
    projet, evenements = lancer(["Créer", "Supprimer notes2.txt"], llm, approbations)

    assert len(approbations.questions) == 1, "un « non » ne se redemande pas"
    assert projet.status is ProjectStatus.FAILED
    assert projet.steps[1].status is StepStatus.FAILED, "une etape refusee ne se coche pas"
    assert "tu as refusé" in (projet.steps[1].error or "")
    assert llm.recettes == 0, "pas de recette : l'utilisateur a deja tranche"
    assert len(projet.steps) == 2, "pas d'etape « Corriger d'apres la recette »"
    assert (Path(projet.workspace_path) / "notes2.txt").exists()
    assert "tu as refusé" in _annonce(evenements)
    # Le modele qui insiste recoit un ARRET, sans nouvelle question.
    assert all("ARRÊT" in r for r in llm.reponses_outils[4:])


def test_une_demande_sans_reponse_arrete_aussi(lancer) -> None:  # noqa: ANN001
    llm = _LLM([_CREER, _SUPPRIMER_EN_INSISTANT], [])
    approbations = _Approbations(None)
    projet, evenements = lancer(["Créer", "Supprimer notes2.txt"], llm, approbations)

    assert len(approbations.questions) == 1
    assert projet.status is ProjectStatus.FAILED
    assert "sans réponse" in (projet.steps[1].error or "")
    assert "tu as refusé" not in _annonce(evenements), "personne n'a refusé"


def test_les_etapes_suivantes_ne_tournent_pas(lancer) -> None:  # noqa: ANN001
    suite = [("write_file", {"path": "x.txt", "content": "x"})]
    llm = _LLM([_CREER, _SUPPRIMER_EN_INSISTANT, suite], [])
    projet, _ = lancer(["Créer", "Supprimer", "Suite"], llm, _Approbations(False))

    assert projet.steps[2].status is StepStatus.PENDING
    assert not (Path(projet.workspace_path) / "x.txt").exists()


def test_la_question_dit_ce_qui_sera_fait(lancer) -> None:  # noqa: ANN001
    approbations = _Approbations(False)
    lancer(["Créer", "Supprimer"], _LLM([_CREER, _SUPPRIMER_EN_INSISTANT], []), approbations)

    assert approbations.questions == ["Supprimer notes2.txt — autoriser ?"]


def test_le_drapeau_du_plan_ne_pose_plus_de_question(lancer) -> None:  # noqa: ANN001
    """Avant : une fenetre pour l'etape, PUIS une pour l'outil.

    Le premier « oui » ne decidait rien.
    """
    llm = _LLM([_CREER, [("delete_file", {"path": "notes2.txt"})]], [{"accepted": True}])
    approbations = _Approbations(True)
    projet, _ = lancer(["Créer", "Supprimer"], llm, approbations, drapeau=True)

    assert approbations.questions == ["Supprimer notes2.txt — autoriser ?"]
    assert projet.status is ProjectStatus.DONE
    assert not (Path(projet.workspace_path) / "notes2.txt").exists()


def test_un_refus_pendant_la_correction_est_la_cause_annoncee(lancer) -> None:  # noqa: ANN001
    refus = {"accepted": False, "missing": ["notes2.txt"], "reason": "notes2.txt existe encore"}
    llm = _LLM([_CREER, _SUPPRIMER_EN_INSISTANT], [refus])
    approbations = _Approbations(False)
    projet, evenements = lancer(["Créer"], llm, approbations)

    assert len(approbations.questions) == 1
    assert llm.recettes == 1, "pas de seconde recette apres un refus"
    assert projet.status is ProjectStatus.FAILED
    assert "tu as refusé" in _annonce(evenements)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

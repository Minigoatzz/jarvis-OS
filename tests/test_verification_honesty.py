# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Une mission est jugee sur son RESULTAT, pas etape par etape.

Les deux missions mortes du 28/09 avaient un travail correct :
- proj_b67093 : script juste, refuse par une regex sur le texte du code ;
- proj_e7b565 : notes2.txt supprime avec ton accord, puis refuse par un juge qui
  ne voyait pas les suppressions et devait « refuser dans le doute ».

Ces tests pilotent le VRAI worker (vrai store, vrai controle objectif) avec un
LLM factice qui joue a la fois l'agent et le juge de recette.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import jarvis.engine.mission.project_store as project_store
from jarvis.engine.mission.project_store import ProjectStore
from jarvis.engine.mission.worker_agent import _TOOL_CATEGORY, _WORKER_TOOLS, WorkerAgent
from jarvis.kernel.schemas import Project, ProjectStatus, Step, StepStatus


class _LLM:
    """`tool_loop` rejoue une liste d'actions ; `complete` rend les verdicts de recette."""

    def __init__(self, actions: list[list[tuple[str, dict]]], verdicts: list[object]) -> None:
        self._actions = list(actions)
        self._verdicts = list(verdicts)
        self.recettes = 0

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        context: str = "",
    ) -> str:
        for name, args in self._actions.pop(0) if self._actions else []:
            await tool_executor(name, args)
        return "fait"

    async def complete(self, messages: list[dict], system: str, **_: object) -> str:
        self.recettes += 1
        verdict = self._verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict if isinstance(verdict, str) else json.dumps(verdict)


def _ok(reason: str = "demande satisfaite") -> dict:
    return {"accepted": True, "missing": [], "reason": reason}


def _refus(*manques: str) -> dict:
    return {"accepted": False, "missing": list(manques), "reason": "incomplet"}


@pytest.fixture
def lancer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Construit et lance un worker isole : jamais le vrai dossier de missions."""
    monkeypatch.setattr(project_store, "WORKSPACE_DIR", tmp_path / "claims")

    def _lancer(mission: str, titres: list[str], llm: _LLM) -> tuple[Project, list[dict]]:
        ws = tmp_path / uuid.uuid4().hex[:6]
        (ws / ".jarvis").mkdir(parents=True)
        projet = Project(
            id="t" + uuid.uuid4().hex[:6],
            title=mission,
            mission=mission,
            workspace_path=str(ws),
            steps=[Step(f"step_{i:03d}", t, "d") for i, t in enumerate(titres, 1)],
        )
        evenements: list[dict] = []
        worker = WorkerAgent(
            project=projet,
            store=ProjectStore(),
            broadcast_event=evenements.append,
            approval_callback=AsyncMock(return_value=True),  # type: ignore[arg-type]
            llm=llm,  # type: ignore[arg-type]
        )
        asyncio.run(worker.run())
        return projet, evenements

    return _lancer


def _annonce(evenements: list[dict]) -> str:
    return [e["text"] for e in evenements if e.get("type") == "message"][-1]


# ── 1. Les deux incidents du 28/09 ──────────────────────────────────────────


def test_un_script_correct_ecrit_a_sa_facon_est_accepte(lancer) -> None:  # noqa: ANN001
    """Plus aucune regex sur le texte du code : la recette juge la demande."""
    script = "primes = generate_primes(100)\nprint(primes)\n"
    llm = _LLM([[("write_file", {"path": "script.py", "content": script})]], [_ok()])
    projet, _ = lancer("écris un script qui calcule les nombres premiers", ["Écrire"], llm)
    assert projet.status is ProjectStatus.DONE


def test_une_suppression_reussie_est_acceptee(lancer) -> None:  # noqa: ANN001
    llm = _LLM(
        [
            [("write_file", {"path": "notes1.txt", "content": "a"}),
             ("write_file", {"path": "notes2.txt", "content": "b"})],
            [("delete_file", {"path": "notes2.txt"})],
        ],
        [_ok()],
    )
    projet, _ = lancer("crée des notes puis supprime notes2.txt", ["Créer", "Supprimer"], llm)
    assert projet.status is ProjectStatus.DONE
    assert not (Path(projet.workspace_path) / "notes2.txt").exists()


# ── 2. Une etape est un progres, pas un verdict ─────────────────────────────


def test_seul_un_defaut_objectif_arrete_une_etape(lancer) -> None:  # noqa: ANN001
    """Un fichier vide, deux fois : la mission s'arrete, les etapes suivantes ne tournent pas."""
    vide = [("write_file", {"path": "notes.txt", "content": ""})]
    llm = _LLM([vide, vide], [])
    projet, _ = lancer("m", ["Écrire", "Suite"], llm)
    assert projet.status is ProjectStatus.FAILED
    assert projet.steps[0].status is StepStatus.FAILED
    assert "vide" in (projet.steps[0].error or "")
    assert projet.steps[1].status is StepStatus.PENDING
    assert llm.recettes == 0, "une etape cassee n'a pas besoin de recette"


# ── 3. Refus, correction, verdict final ─────────────────────────────────────


def test_un_refus_argumente_declenche_une_correction_visible(lancer) -> None:  # noqa: ANN001
    llm = _LLM(
        [
            [("write_file", {"path": "rapport.md", "content": "# Semaine"})],
            [("write_file", {"path": "semaine.md", "content": "# Semaine\n## Lundi"})],
        ],
        [_refus("semaine.md absent"), _ok()],
    )
    projet, _ = lancer("rédige semaine.md", ["Rédiger"], llm)

    assert projet.status is ProjectStatus.DONE
    assert projet.steps[-1].title == "Corriger d'après la recette"
    assert "semaine.md absent" in projet.steps[-1].description
    assert llm.recettes == 2


def test_deux_refus_font_echouer_la_mission_avec_la_raison(lancer) -> None:  # noqa: ANN001
    llm = _LLM(
        [[("write_file", {"path": "a.md", "content": "x"})],
         [("write_file", {"path": "a.md", "content": "y"})]],
        [_refus("semaine.md absent"), _refus("semaine.md toujours absent")],
    )
    projet, evenements = lancer("rédige semaine.md", ["Rédiger"], llm)

    assert projet.status is ProjectStatus.FAILED
    assert "semaine.md toujours absent" in _annonce(evenements)
    assert sum(s.title == "Corriger d'après la recette" for s in projet.steps) == 1


def test_un_juge_en_panne_ne_lance_pas_de_correction(lancer) -> None:  # noqa: ANN001
    llm = _LLM([[("write_file", {"path": "a.md", "content": "x"})]],
               [ConnectionError("Ollama injoignable")])
    projet, evenements = lancer("m", ["Écrire"], llm)

    assert projet.status is ProjectStatus.FAILED
    assert all(s.title != "Corriger d'après la recette" for s in projet.steps)
    assert "Ollama injoignable" in _annonce(evenements)


# ── 4. Les preuves consignees ───────────────────────────────────────────────


def test_une_commande_est_consignee_avec_son_code_et_sa_sortie(tmp_path: Path) -> None:
    projet = Project(id="p", title="t", mission="m", workspace_path=str(tmp_path))
    worker = WorkerAgent(
        project=projet, store=MagicMock(), broadcast_event=MagicMock(),
        approval_callback=AsyncMock(), llm=MagicMock(),  # type: ignore[arg-type]
    )
    worker._log = AsyncMock()  # type: ignore[method-assign]
    worker._cli_tool = MagicMock()
    worker._cli_tool.execute = AsyncMock(
        return_value={"success": True, "stdout": "[2, 3, 5]", "stderr": "", "returncode": 0}
    )

    asyncio.run(worker._tool_executor("execute_cli", {"command": "python3 script.py"}))

    data = worker._log.call_args.kwargs["data"]
    assert data == {"returncode": 0, "output": "[2, 3, 5]"}


# ── 5. Supprimer : possible, et toujours sous approbation ───────────────────


def test_le_worker_dispose_d_un_outil_de_suppression() -> None:
    assert "delete_file" in [t["name"] for t in _WORKER_TOOLS]


def test_chaque_suppression_passe_par_la_categorie_file_delete() -> None:
    """file_delete vaut ASK par defaut : le gate demande l'approbation."""
    assert _TOOL_CATEGORY["delete_file"] == "file_delete"


def test_sans_gouvernance_la_suppression_est_refusee(tmp_path: Path) -> None:
    (tmp_path / "notes2.txt").write_text("x")
    projet = Project(id="p", title="t", mission="m", workspace_path=str(tmp_path))
    worker = WorkerAgent(
        project=projet, store=MagicMock(), broadcast_event=MagicMock(),
        approval_callback=AsyncMock(), llm=MagicMock(),  # type: ignore[arg-type]
    )
    worker._log = AsyncMock()  # type: ignore[method-assign]
    worker._governance = None

    resultat = asyncio.run(worker._tool_executor("delete_file", {"path": "notes2.txt"}))

    assert "REFUS" in resultat.upper()
    assert (tmp_path / "notes2.txt").exists()

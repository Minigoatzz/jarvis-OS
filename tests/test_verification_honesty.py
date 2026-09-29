# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Une verification doit pouvoir echouer, et dire pourquoi.

Deux missions du 28/09 au soir, deux defauts de la meme famille :

- proj_e7b565 : « Supprimer notes2.txt » verifie par
  `test -f notes2.txt && echo ... || echo ...` — rend 0 dans les deux cas. Le
  worker, sans outil de suppression, a affirme l'avoir fait ; la verification
  a valide le mensonge ; notes2.txt etait encore la.
- proj_b67093 : script correct, mais la verification exigeait le texte
  litteral `print(generate_primes(100))`. Le worker recevait « 0 ligne(s)
  correspondent au motif » — sans le motif. Ses deux relances ont echoue
  a l'identique.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from jarvis.engine.mission.quality_checker import QualityChecker
from jarvis.engine.mission.verifier import Verifier
from jarvis.engine.mission.worker_agent import (
    _TOOL_CATEGORY,
    _WORKER_TOOLS,
    WorkerAgent,
)
from jarvis.kernel.schemas import Project, Step, StepStatus


class _Juge:
    """Couche semantique factice : on controle son verdict, on compte ses appels."""

    def __init__(self, verified: bool, notes: str = "") -> None:
        self._raw = json.dumps({"verified": verified, "issues": [], "notes": notes})
        self.calls = 0

    async def complete(self, *a: object, **k: object) -> str:
        self.calls += 1
        return self._raw

    async def health_check(self) -> bool:
        return True


def _setup(commande: str, fichiers: dict[str, str]) -> tuple[Project, Step, str]:
    ws = tempfile.mkdtemp()
    for nom, contenu in fichiers.items():
        (Path(ws) / nom).write_text(contenu, encoding="utf-8")
    step = Step("s1", "étape", "d", success_criterion="c", verification_command=commande)
    return Project(id="p", title="t", mission="m", steps=[step], workspace_path=ws), step, ws


def _verifier(ws: str, juge: _Juge, cli: object = None) -> Verifier:
    return Verifier(QualityChecker(ws), juge, cli_executor=cli, workspace_path=ws)  # type: ignore[arg-type]


# ── 1. Une commande qui ne peut pas echouer n'est pas une verification ──────


def test_une_tautologie_ne_valide_pas_un_travail_non_fait() -> None:
    """notes2.txt existe encore : le juge semantique doit trancher, pas `|| echo`."""
    project, step, ws = _setup(
        "test -f notes2.txt && echo 'existe' || echo 'supprimé'",
        {"notes2.txt": "encore la\n"},
    )
    juge = _Juge(False, "notes2.txt n'a pas été supprimé")

    async def _cli_toujours_ok(cmd: str, t: int) -> dict:  # noqa: ASYNC109
        return {"success": True, "stdout": "notes2.txt existe", "stderr": "", "returncode": 0}

    verdict = asyncio.run(_verifier(ws, juge, _cli_toujours_ok).verify(project, step, []))

    assert verdict.verified is False, "la tautologie avait valide le mensonge"
    assert juge.calls == 1, "c'est la couche semantique qui doit juger"
    assert "notes2.txt" in verdict.notes


# ── 2. Un echec dit quelle commande, et pourquoi ────────────────────────────


def test_la_relance_sait_quelle_verification_viser() -> None:
    script = "primes = generate_primes(100)\nprint(primes)\n"
    commande = r"grep -E 'print\(generate_primes\(100\)' script.py"
    project, step, ws = _setup(commande, {"script.py": script})

    verdict = asyncio.run(_verifier(ws, _Juge(True)).verify(project, step, []))

    assert verdict.verified is False
    consigne = " ".join(verdict.issues)
    assert commande in consigne, "sans la commande, le worker corrige a l'aveugle"
    assert "0 ligne(s)" in consigne
    assert commande in verdict.notes, "l'utilisateur doit la voir aussi"


def test_une_commande_executee_qui_echoue_se_nomme_aussi() -> None:
    project, step, ws = _setup("pytest -x", {"ok.py": "x = 1\n"})

    async def _cli_echec(cmd: str, t: int) -> dict:  # noqa: ASYNC109
        return {"success": False, "stdout": "", "stderr": "1 failed", "returncode": 1}

    verdict = asyncio.run(_verifier(ws, _Juge(True), _cli_echec).verify(project, step, []))
    assert any("pytest -x" in i and "1 failed" in i for i in verdict.issues)


# ── 3. L'erreur finale de l'etape porte la raison ───────────────────────────


def test_l_erreur_de_l_etape_cite_ce_que_le_verificateur_a_vu() -> None:
    """L'utilisateur ne lisait que « Verification non concluante apres 2 essais »."""
    ws = tempfile.mkdtemp()
    step = Step("s1", "Vérification", "d", success_criterion="c")
    project = Project(id="p", title="t", mission="m", steps=[step], workspace_path=ws)
    worker = WorkerAgent(
        project=project,
        store=MagicMock(),
        broadcast_event=MagicMock(),
        approval_callback=AsyncMock(),  # type: ignore[arg-type]
        llm=MagicMock(),
    )
    worker._log = AsyncMock()  # type: ignore[method-assign]
    worker._run_step_llm = AsyncMock(return_value="fait")  # type: ignore[method-assign]
    refus = MagicMock(verified=False, unverified=False, layer="semantic", issues=[],
                      notes="notes2.txt n'a pas été supprimé")
    worker._verifier = MagicMock()
    worker._verifier.verify = AsyncMock(return_value=refus)

    asyncio.run(worker._execute_with_verification(step))  # la boucle verifier + relances

    assert step.status is StepStatus.FAILED
    assert "notes2.txt n'a pas été supprimé" in (step.error or "")


# ── 4. Supprimer est possible, et toujours sous approbation ─────────────────


def test_le_worker_dispose_d_un_outil_de_suppression() -> None:
    """Sans lui, toute demande de suppression etait vouee a l'echec — ou au mensonge."""
    assert "delete_file" in [t["name"] for t in _WORKER_TOOLS]


def test_chaque_suppression_passe_par_la_categorie_file_delete() -> None:
    """file_delete vaut ASK par defaut : le gate demande l'approbation."""
    assert _TOOL_CATEGORY["delete_file"] == "file_delete"


def test_sans_gouvernance_la_suppression_est_refusee() -> None:
    """Le gate laisse tout passer sans gouvernance : pour supprimer, on l'exige."""
    ws = tempfile.mkdtemp()
    (Path(ws) / "notes2.txt").write_text("x")
    project = Project(id="p", title="t", mission="m", workspace_path=ws)
    worker = WorkerAgent(
        project=project,
        store=MagicMock(),
        broadcast_event=MagicMock(),
        approval_callback=AsyncMock(),  # type: ignore[arg-type]
        llm=MagicMock(),
    )
    worker._log = AsyncMock()  # type: ignore[method-assign]
    worker._governance = None

    resultat = asyncio.run(worker._tool_executor("delete_file", {"path": "notes2.txt"}))

    assert "REFUS" in resultat.upper()
    assert (Path(ws) / "notes2.txt").exists()

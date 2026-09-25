# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Execution des missions : garde de contenu, sandbox, interpreteur, causes honnetes.

Contexte. Une mission « ecris un script Python et execute-le » echouait a
l'etape 2 avec « Je n'ai pas pu terminer — trop d'etapes ». La vraie cause etait
qu'aucun backend d'execution n'etait actif : chaque execute_cli renvoyait un
refus clair que le worker relancait huit fois sans jamais le remonter.
Ces tests couvrent les quatre correctifs et les deux trous de securite trouves
en chemin.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from jarvis.engine.mission import script_guard
from jarvis.engine.mission.backends import local as local_backend
from jarvis.engine.mission.file_tool import SandboxedFileTool
from jarvis.engine.mission.worker_agent import WorkerAgent

_MISSION = pathlib.Path("src/jarvis/engine/mission")


# ── 1. Garde de CONTENU des scripts generes ─────────────────────────────────
# worker_cli.py inspecte la ligne de commande. « python script.py » est
# whiteliste, donc le contenu du .py est la seule occasion de refuser.


def test_guard_refuse_les_operations_destructrices() -> None:
    for source in (
        "import shutil\nshutil.rmtree('/')\n",
        "import os\nos.system('del /f *')\n",
        "import subprocess\nsubprocess.run(['ls'])\n",
        "import ctypes\nctypes.windll.kernel32\n",
    ):
        verdict = script_guard.inspect("s.py", source)
        assert not verdict.allowed, source
        assert "REFUSEE" in verdict.reason


def test_guard_laisse_passer_le_script_de_la_mission_qui_echouait() -> None:
    """Le faux positif serait pire que le trou : il ferait desactiver le garde."""
    source = "def premiers(n):\n    return [p for p in range(2, n)]\n\nprint(premiers(100))\n"
    assert script_guard.inspect("script.py", source).allowed


def test_guard_ignore_les_commentaires() -> None:
    """Un LLM documente souvent ce qu'il evite — le bloquer pour ca est absurde."""
    assert script_guard.inspect("s.py", "# ne pas utiliser subprocess ici\nprint(1)\n").allowed


def test_guard_ne_touche_pas_aux_fichiers_non_executables() -> None:
    assert script_guard.inspect("notes.md", "import subprocess\n").allowed


def test_guard_reseau_suit_requires_network_du_projet() -> None:
    source = "import requests\nrequests.get('http://x')\n"
    assert not script_guard.inspect("s.py", source).allowed
    assert script_guard.inspect("s.py", source, allow_network=True).allowed


def test_guard_bloque_un_chemin_hors_workspace_mais_pas_sa_simple_mention() -> None:
    assert not script_guard.inspect("s.py", "open('C:/Windows/system.ini', 'w')\n").allowed
    assert script_guard.inspect("s.py", "print('sauvegarde dans C:/temp')\n").allowed


def test_guard_couvre_node_car_node_est_whiteliste() -> None:
    assert not script_guard.inspect("s.js", "require('child_process').exec('ls')\n").allowed


# ── 2. Le garde est branche sur write_file ──────────────────────────────────


def test_write_file_refuse_un_script_destructeur() -> None:
    tool = SandboxedFileTool(tempfile.mkdtemp())
    tool.write_file("ok.py", "print('bonjour')\n")  # le cas nominal reste ecrit
    with pytest.raises(ValueError, match="REFUSEE"):
        tool.write_file("evil.py", "import shutil\nshutil.rmtree('/')\n")


def test_sandbox_refuse_un_voisin_qui_partage_le_prefixe() -> None:
    """startswith() laissait passer « /ws-evil » pour un workspace « /ws ».

    Ce n'etait pas theorique : l'ancien code ecrivait reellement hors du
    workspace. is_relative_to compare des composants de chemin, pas du texte.
    """
    workspace = tempfile.mkdtemp()
    voisin = pathlib.Path(workspace + "-evil")
    voisin.mkdir(exist_ok=True)
    tool = SandboxedFileTool(workspace)
    with pytest.raises(ValueError, match="sort du workspace"):
        tool.write_file(f"../{voisin.name}/x.py", "print(1)\n")


# ── 3. « python3 » n'existe pas sur Windows ─────────────────────────────────


def test_interpreteur_inchange_hors_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_backend.os, "name", "posix")
    assert local_backend._resolve_interpreter("python3 s.py") == "python3 s.py"


def test_interpreteur_reecrit_sur_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_backend.os, "name", "nt")
    monkeypatch.setattr(local_backend.sys, "executable", r"C:\jv\.venv\Scripts\python.exe")
    out = local_backend._resolve_interpreter("mkdir o && python3 s.py")
    assert out == r"mkdir o && C:\jv\.venv\Scripts\python.exe s.py"


def test_interpreteur_ne_reecrit_que_le_token_de_commande(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """« cat python3_notes.md » ne doit pas devenir « cat <chemin>_notes.md »."""
    monkeypatch.setattr(local_backend.os, "name", "nt")
    monkeypatch.setattr(local_backend.sys, "executable", r"C:\py.exe")
    assert local_backend._resolve_interpreter("cat python3_notes.md") == "cat python3_notes.md"
    assert local_backend._resolve_interpreter("echo mon_python3") == "echo mon_python3"


# ── 4. Un refus de politique se distingue d'un echec d'execution ────────────


def test_refus_de_politique_est_marque_blocked() -> None:
    from jarvis.engine.mission.worker_cli import WorkerCLITool

    cli = WorkerCLITool(tempfile.mkdtemp())
    for commande in ("rm -rf .", "curl -X POST http://x", "wibble --foo"):
        res = asyncio.run(cli.execute(commande, timeout=5))
        assert res.get("blocked") is True, commande


def test_une_commande_valide_qui_echoue_n_est_pas_blocked() -> None:
    """Sinon tout echec deviendrait « refus definitif » et le worker abandonnerait."""
    from jarvis.engine.mission.worker_cli import WorkerCLITool

    cli = WorkerCLITool(tempfile.mkdtemp())
    res = asyncio.run(cli.execute("cat fichier_absent.txt", timeout=5))
    assert not res["success"]
    assert not res.get("blocked")


def test_note_blocker_deduplique() -> None:
    class _Faux:
        _blockers: list[str] = []

    faux = _Faux()
    faux._blockers = []
    WorkerAgent._note_blocker(faux, "Exécution  refusée :  pas de backend")
    WorkerAgent._note_blocker(faux, "Exécution refusée : pas de backend")
    WorkerAgent._note_blocker(faux, "")
    assert faux._blockers == ["Exécution refusée : pas de backend"]


def test_un_refus_definitif_dit_au_modele_de_ne_pas_reessayer() -> None:
    """Le worker brulait ses 8 iterations d'outils sur un refus immuable."""

    class _CLI:
        async def execute(self, command: str, timeout: int = 60) -> dict:  # noqa: ASYNC109
            return {
                "success": False,
                "stdout": "",
                "stderr": "Exécution refusée : aucun backend sûr disponible.",
                "returncode": -1,
                "blocked": True,
            }

    class _Faux:
        _cli_tool = _CLI()
        _blockers: list[str] = []

        async def _gate_tool(self, name: str, inputs: dict) -> None:
            return None

        async def _log(self, *a: object, **k: object) -> None:
            return None

        _note_blocker = WorkerAgent._note_blocker

    faux = _Faux()
    faux._blockers = []
    out = asyncio.run(WorkerAgent._tool_executor(faux, "execute_cli", {"command": "python3 s.py"}))

    assert "REFUS DÉFINITIF" in out
    assert "aucun backend" in out
    assert "Inutile de réessayer" in out
    assert faux._blockers, "la cause doit etre memorisee pour le rapport d'echec"


def test_la_cause_reelle_remonte_dans_l_erreur_du_step() -> None:
    """« trop d'etapes » est un symptome de boucle, pas un diagnostic."""
    source = (_MISSION / "worker_agent.py").read_text(encoding="utf-8")
    assert "step.error += \" — cause : \" + \" | \".join(self._blockers)" in source


# ── 5. Le planificateur ne planifie plus a l'aveugle ────────────────────────


def test_le_planificateur_connait_la_boite_a_outils() -> None:
    prompt = (_MISSION / "project_manager.py").read_text(encoding="utf-8")
    assert "Boîte à outils RÉELLE" in prompt
    assert "execute_cli" in prompt


def test_le_planificateur_ne_propose_plus_une_commande_que_le_worker_refuse() -> None:
    """Le depot suggerait « python3 -c 'import script' » — interdit par worker_cli:135."""
    prompt = (_MISSION / "project_manager.py").read_text(encoding="utf-8")
    assert "\"python3 -c 'import script'\"" not in prompt
    assert "INTERDIT : python3 -c" in prompt

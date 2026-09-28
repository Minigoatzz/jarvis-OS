# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Chemins de fichiers d'une mission : format POSIX et fichiers internes.

files_created contenait « rapports\\RAPPORT.md » sur Windows, colle ensuite dans
l'URL /api/projects/{id}/files/{chemin} par le dashboard — lien casse.
"""

from __future__ import annotations

import pathlib
import tempfile

from jarvis.engine.mission.file_tool import SandboxedFileTool
from jarvis.engine.mission.quality_checker import QualityChecker


def _workspace(prefixe: str = "") -> pathlib.Path:
    racine = pathlib.Path(tempfile.mkdtemp()) / prefixe / "proj"
    racine.mkdir(parents=True)
    return racine


def test_les_chemins_sont_posix() -> None:
    ws = _workspace()
    outil = SandboxedFileTool(str(ws))
    outil.write_file("rapports/RAPPORT.md", "# r")
    assert outil.list_files() == ["rapports/RAPPORT.md"]


def test_les_fichiers_internes_sont_exclus() -> None:
    ws = _workspace()
    outil = SandboxedFileTool(str(ws))
    outil.write_file("bonjour.txt", "x")
    (ws / ".jarvis").mkdir()
    (ws / ".jarvis" / "state.json").write_text("{}")
    (ws / ".jarvis_rpc" / "ab").mkdir(parents=True)
    (ws / ".jarvis_rpc" / "ab" / "user_script.py").write_text("x")
    assert outil.list_files() == ["bonjour.txt"]


def test_un_workspace_sous_un_dossier_nomme_jarvis_reste_visible() -> None:
    """L'ancien filtre testait le chemin ABSOLU : tout disparaissait."""
    ws = _workspace("dossier.jarvis_perso")
    outil = SandboxedFileTool(str(ws))
    outil.write_file("bonjour.txt", "x")
    assert outil.list_files() == ["bonjour.txt"]


def test_le_controle_qualite_suit_la_meme_regle() -> None:
    ws = _workspace("dossier.jarvis_perso")
    (ws / "rapports").mkdir()
    (ws / "rapports" / "RAPPORT.md").write_text("# r")
    (ws / ".jarvis").mkdir()
    (ws / ".jarvis" / "state.json").write_text("{}")
    chemins = [f["path"] for f in QualityChecker(str(ws)).list_all_files()]
    assert chemins == ["rapports/RAPPORT.md"]

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


# ── Un dossier qui porte un nom de fichier ──────────────────────────────────
# proj_342e5d (28/09) : le modele a appele create_directory("RAPPORT.md"). Sous
# Windows, chaque write_file("RAPPORT.md") a ensuite leve « [Errno 13]
# Permission denied » — six fois. Le message ne disait pas « c'est un dossier »,
# list_files ne montre pas les dossiers, et le worker n'a aucun outil pour en
# supprimer un : il ne pouvait ni comprendre ni reparer.


def test_create_directory_refuse_un_nom_de_fichier() -> None:
    import pytest

    outil = SandboxedFileTool(str(_workspace()))
    with pytest.raises(ValueError, match="write_file"):
        outil.create_directory("RAPPORT.md")


def test_create_directory_accepte_un_vrai_dossier() -> None:
    ws = _workspace()
    SandboxedFileTool(str(ws)).create_directory("rapport")
    assert (ws / "rapport").is_dir()


def test_un_dossier_vide_au_nom_du_fichier_est_remplace() -> None:
    """Sans contenu, rien a perdre : la mission redevient reparable par un Retry."""
    ws = _workspace()
    (ws / "RAPPORT.md").mkdir()
    SandboxedFileTool(str(ws)).write_file("RAPPORT.md", "# Rapport\n")
    assert (ws / "RAPPORT.md").is_file()


def test_un_dossier_non_vide_n_est_jamais_ecrase() -> None:
    import pytest

    ws = _workspace()
    (ws / "docs.md").mkdir()
    (ws / "docs.md" / "important.txt").write_text("a garder")
    with pytest.raises(ValueError, match="DOSSIER non vide"):
        SandboxedFileTool(str(ws)).write_file("docs.md", "x")
    assert (ws / "docs.md" / "important.txt").read_text() == "a garder"


def test_le_dashboard_n_affiche_plus_un_fichier_comme_un_dossier() -> None:
    """files_created ne contient que des fichiers : 📁 n'y est jamais juste."""
    js = pathlib.Path("src/jarvis/interfaces/ui/static/dashboard.js").read_text(encoding="utf-8")
    ligne = next(l for l in js.splitlines() if "mp-file-icon" in l)
    assert "📁" not in ligne

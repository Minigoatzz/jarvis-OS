# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""
QualityChecker — vérifications automatiques de qualité post-étape et fin de projet.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from loguru import logger

from jarvis.kernel.error_collector import collector  # jrv: autofix

# Fichiers vides par nature : les signaler ferait échouer toute mission Python
# qui crée un paquet (« __init__.py »).
_EMPTY_BY_NATURE = frozenset({"__init__.py", ".gitkeep"})


class QualityChecker:
    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path).resolve()

    # ── File checks ───────────────────────────────────────────────────────────

    def check_file_not_empty(self, file_path: str) -> bool:
        target = self._workspace / file_path
        return target.exists() and target.stat().st_size > 0

    def list_all_files(self) -> list[dict]:
        """Liste tous les fichiers du workspace (hors .jarvis) avec métadonnées."""
        files = []
        for f in self._workspace.rglob("*"):
            relative = f.relative_to(self._workspace)
            # Même règle que SandboxedFileTool : chemin relatif, séparateur POSIX.
            if f.is_file() and not any(p.startswith(".jarvis") for p in relative.parts):
                rel = relative.as_posix()
                files.append(
                    {
                        "path": rel,
                        "size": f.stat().st_size,
                        "extension": f.suffix,
                    }
                )
        return files

    # ── HTML reference check ──────────────────────────────────────────────────

    def check_html_references(self, html_path: str) -> list[str]:
        """Vérifie que tous les fichiers référencés dans un HTML existent.

        Retourne les manquants.
        """
        html_file = self._workspace / html_path
        if not html_file.exists():
            return [f"HTML introuvable: {html_path}"]

        content = html_file.read_text(encoding="utf-8", errors="replace")
        missing = []

        for ref in re.findall(r'href=["\']([^"\']+\.css)["\']', content):
            if not ref.startswith(("http://", "https://", "//", "data:")):
                target = (self._workspace / Path(html_path).parent / ref).resolve()
                if not target.exists():
                    missing.append(f"CSS manquant: {ref}")

        for ref in re.findall(r'src=["\']([^"\']+\.js)["\']', content):
            if not ref.startswith(("http://", "https://", "//", "data:")):
                target = (self._workspace / Path(html_path).parent / ref).resolve()
                if not target.exists():
                    missing.append(f"JS manquant: {ref}")

        for ref, _ in re.findall(r'src=["\']([^"\']+\.(png|jpg|jpeg|svg|webp|gif))["\']', content):
            if not ref.startswith(("http://", "https://", "//", "data:")):
                target = (self._workspace / Path(html_path).parent / ref).resolve()
                if not target.exists():
                    missing.append(f"Image manquante: {ref}")

        return missing

    # ── Python syntax check ───────────────────────────────────────────────────

    def check_python_syntax(self, py_path: str) -> dict:
        target = self._workspace / py_path
        if not target.exists():
            return {"valid": False, "error": "Fichier introuvable"}
        try:
            ast.parse(target.read_text(encoding="utf-8", errors="replace"))
            return {"valid": True, "error": None}
        except SyntaxError as e:
            collector.error("JRV-MSN-001", "JRV-MSN-001", cause=e)
            return {"valid": False, "error": str(e)}

    # ── Full report ───────────────────────────────────────────────────────────

    def _issues(self, *, cross_file: bool) -> list[str]:
        """Problèmes objectifs sur TOUS les fichiers du workspace.

        `cross_file` ajoute les contrôles qui dépendent d'autres fichiers (références
        d'un HTML). Ils ne valent qu'une fois la mission finie : un plan normal écrit
        index.html AVANT style.css, et les vérifier à chaque étape faisait échouer
        l'étape du HTML. Un seul endroit pour ces règles — generate_report et
        check_step_output en tenaient chacun une copie, qui avaient divergé.
        """
        issues: list[str] = []
        for f in self.list_all_files():
            if f["size"] == 0 and Path(f["path"]).name not in _EMPTY_BY_NATURE:
                issues.append(f"Fichier vide : {f['path']}")
            if f["extension"] == ".py":
                result = self.check_python_syntax(f["path"])
                if not result["valid"]:
                    issues.append(f"Syntaxe Python invalide dans {f['path']} : {result['error']}")
            if cross_file and f["extension"] == ".html":
                issues.extend(self.check_html_references(f["path"]))
        return issues

    def generate_report(self) -> dict:
        """Rapport de qualité complet, fin de mission : fichiers et problèmes."""
        files = self.list_all_files()
        issues = self._issues(cross_file=True)
        valid = not issues
        logger.info("QualityChecker report", files=len(files), issues=len(issues), valid=valid)
        return {"files": files, "issues": issues, "valid": valid}

    def check_step_output(self) -> list[str]:
        """Contrôle après une étape : ce qui doit être vrai à TOUT moment.

        Tous les fichiers, pas seulement les nouveaux : l'ancienne version ne
        voyait pas un fichier existant réécrit avec une erreur de syntaxe.
        """
        return self._issues(cross_file=False)

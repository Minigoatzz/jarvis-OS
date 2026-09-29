# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Outil fichiers sandboxé — toutes les opérations confinées au workspace du projet."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from jarvis.engine.mission import script_guard

# Extensions qui désignent sans ambiguïté un FICHIER. Un dossier ainsi nommé est
# une erreur du modèle, pas une intention : cf. create_directory().
_FILE_SUFFIXES = frozenset(
    {
        ".md", ".txt", ".py", ".js", ".ts", ".json", ".csv", ".html", ".htm", ".css",
        ".yaml", ".yml", ".xml", ".toml", ".ini", ".cfg", ".log", ".sql", ".sh",
        ".ps1", ".bat", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
        ".docx", ".xlsx", ".pptx", ".zip",
    }
)


def _is_internal(rel: Path) -> bool:
    """Vrai pour les fichiers internes de Jarvis (.jarvis/, .jarvis_rpc/…).

    Testé sur le chemin RELATIF au workspace. L'ancien filtre
    `".jarvis" not in str(p)` portait sur le chemin absolu : un workspace
    rangé sous un dossier dont le nom contient « .jarvis » aurait vu tous ses
    fichiers disparaître.
    """
    return any(part.startswith(".jarvis") for part in rel.parts)


class SandboxedFileTool:
    def __init__(self, workspace_path: str, *, allow_network: bool = False) -> None:
        self._workspace = Path(workspace_path).resolve()
        # Vient de project.requires_network : conditionne le garde réseau de
        # script_guard, qui refuse un import requests dans un projet offline.
        self._allow_network = allow_network

    def _safe_path(self, relative_path: str) -> Path:
        """Vérifie que le chemin résolu reste dans le workspace. Lève ValueError sinon."""
        target = (self._workspace / relative_path).resolve()
        # is_relative_to, pas startswith : un workspace « /ws » laissait passer
        # « /ws-evil », qui partage le préfixe sans être dedans.
        if not target.is_relative_to(self._workspace):
            logger.error("SANDBOX VIOLATION", path=relative_path, target=str(target))
            raise ValueError(f"ACCÈS REFUSÉ : '{relative_path}' sort du workspace autorisé.")
        return target

    def read_file(self, path: str) -> str:
        target = self._safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"Fichier non trouvé : {path}")
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        target = self._safe_path(path)
        if target.is_dir():
            # Sous Windows, écrire sur un dossier lève « [Errno 13] Permission
            # denied » — message trompeur : le 28/09, un dossier RAPPORT.md créé
            # par erreur a fait échouer six écritures de suite, et le modèle n'a
            # jamais compris pourquoi. Vide, c'est un artefact sans contenu : on
            # le remplace. Non vide, on le dit en clair plutôt que d'effacer.
            if any(target.iterdir()):
                raise ValueError(
                    f"« {path} » est un DOSSIER non vide, pas un fichier : impossible "
                    f"d'y écrire. Choisis un autre nom de fichier."
                )
            target.rmdir()
            logger.warning("Dossier vide remplacé par un fichier", path=path)
        # Le garde CLI n'inspecte que la ligne de commande ; « python script.py »
        # est whitelisté, donc le CONTENU du script est la seule occasion de
        # refuser un rmtree ou un subprocess halluciné. C'est ici ou nulle part.
        verdict = script_guard.inspect(path, content, allow_network=self._allow_network)
        if not verdict.allowed:
            logger.error("SCRIPT GUARD", path=path, reason=verdict.reason[:120])
            raise ValueError(verdict.reason)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.info("Sandbox write", path=path, chars=len(content))
        return f"Fichier écrit : {path} ({len(content)} caractères)"

    def list_files(self, directory: str = ".") -> list[str]:
        """Fichiers du workspace, en chemins relatifs POSIX (« a/b.md »).

        POSIX et non str() : sur Windows str() donnait « rapports\\RAPPORT.md »,
        stocké tel quel dans files_created, puis collé dans l'URL
        /api/projects/{id}/files/{chemin} par le dashboard — lien cassé.
        """
        target = self._safe_path(directory)
        if not target.is_dir():
            return []
        files: list[str] = []
        for p in sorted(target.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self._workspace)
            if _is_internal(rel):
                continue
            files.append(rel.as_posix())
        return files

    def delete_file(self, path: str) -> str:
        """Supprime UN fichier du workspace. Jamais un dossier, jamais un fichier interne.

        Exposé au worker (outil delete_file, catégorie file_delete → approbation).
        _safe_path confine au workspace, mais .jarvis/state.json et le journal y
        sont aussi : sans ce garde, le worker aurait pu effacer son propre état.
        """
        target = self._safe_path(path)
        if _is_internal(target.relative_to(self._workspace)):
            raise ValueError(f"« {path} » est un fichier interne de Jarvis : suppression refusée.")
        if not target.exists():
            return f"Fichier inexistant : {path}"
        if target.is_dir():
            raise ValueError(
                f"« {path} » est un dossier : delete_file ne supprime que des fichiers."
            )
        target.unlink()
        logger.info("Sandbox delete", path=path)
        return f"Supprimé : {path}"

    def create_directory(self, path: str) -> str:
        target = self._safe_path(path)
        # Un « dossier » nommé RAPPORT.md bloque ensuite toute écriture du
        # fichier du même nom. write_file crée déjà les dossiers parents : ce
        # dossier-là n'a aucun usage légitime.
        if target.suffix.lower() in _FILE_SUFFIXES:
            raise ValueError(
                f"« {path} » est un nom de FICHIER, pas de dossier. Utilise "
                f"write_file pour le créer : il crée lui-même les dossiers parents."
            )
        target.mkdir(parents=True, exist_ok=True)
        return f"Répertoire créé : {path}"

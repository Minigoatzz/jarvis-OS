# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

from pathlib import Path

from loguru import logger

from jarvis.kernel.error_collector import collector  # jrv: autofix


class MemoryIndex:
    """Lecture/écriture de MEMORY.md — l'index des pointeurs mémoire.

    MEMORY.md ne contient que des pointeurs, jamais de contenu direct.
    """

    _TEMPLATE = (
        "# MEMORY.md\n\n"
        "Index des pointeurs mémoire. Ce fichier ne contient que des "
        "pointeurs vers les fichiers thématiques détaillés (topics/) et les "
        "transcripts de session (sessions/), jamais de contenu direct.\n"
    )

    def __init__(self, memory_dir: Path) -> None:
        self._path = memory_dir / "MEMORY.md"
        # Bootstrap au premier lancement : sans ce bloc, memory_data/ (et donc
        # MEMORY.md) n'existe pas tant qu'aucun pointeur n'a été ajouté via
        # add_pointer() — et Agent._build_system() appelle read() à CHAQUE
        # tour de conversation pour construire le prompt système. Résultat
        # observé en usage réel : un FileNotFoundError rattrapé (donc pas de
        # crash) mais loggé en WARNING à chaque tour, dès la première
        # conversation — 20+ occurrences en une session avant le premier
        # add_pointer(). SessionStore et TopicStore, les deux classes sœurs de
        # ce module, créent déjà leur propre répertoire ainsi en __init__ ;
        # MemoryIndex ne le faisait pas et ne crée pas non plus le fichier
        # lui-même, contrairement à un répertoire vide qui est un état de
        # démarrage normal.
        try:
            memory_dir.mkdir(parents=True, exist_ok=True)
            if not self._path.exists():
                self._path.write_text(self._TEMPLATE, encoding="utf-8")
                logger.info("MemoryIndex bootstrap : MEMORY.md créé", path=str(self._path))
        except OSError as e:
            # Ne bloque pas le démarrage : read()/_write() gèrent déjà l'échec
            # (JRV-MEM-001) si le problème persiste (ex. permissions).
            collector.warning("JRV-MEM-001", "JRV-MEM-001", cause=e)
            logger.error("MemoryIndex bootstrap failed", error=str(e))

    def read(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8")
        except OSError as e:
            collector.warning("JRV-MEM-001", "JRV-MEM-001", cause=e)
            logger.error("MemoryIndex.read failed", error=str(e))
            return ""

    def add_pointer(self, section: str, key: str, filepath: str, description: str) -> None:
        """Ajoute ou met à jour un pointeur dans MEMORY.md.

        Si la clé existe déjà, la ligne est mise à jour sur place.
        Si la section existe, le pointeur y est ajouté.
        Sinon, une nouvelle section est créée en fin de fichier.
        """
        content = self.read()
        pointer_line = f"- {key}: `{filepath}` — {description}"
        lines = content.splitlines()

        # Mise à jour si le pointeur existe déjà
        for i, line in enumerate(lines):
            if line.strip().startswith(f"- {key}:"):
                lines[i] = pointer_line
                self._write("\n".join(lines))
                logger.debug("MemoryIndex pointer updated", key=key)
                return

        # Insertion dans la bonne section
        in_section = False
        for i, line in enumerate(lines):
            if line.strip() == f"## {section}":
                in_section = True
            elif in_section and (line.startswith("## ") or line.startswith("# ")):
                lines.insert(i, pointer_line)
                self._write("\n".join(lines))
                logger.debug("MemoryIndex pointer added", key=key, section=section)
                return
            elif in_section and i == len(lines) - 1:
                lines.append(pointer_line)
                self._write("\n".join(lines))
                logger.debug("MemoryIndex pointer added (end of section)", key=key)
                return

        # Section introuvable → créer en fin de fichier
        lines.extend(["", f"## {section}", pointer_line])
        self._write("\n".join(lines))
        logger.debug("MemoryIndex new section created", section=section, key=key)

    def _write(self, content: str) -> None:
        try:
            self._path.write_text(content + "\n", encoding="utf-8")
        except OSError as e:
            collector.warning("JRV-MEM-001", "JRV-MEM-001", cause=e)
            logger.error("MemoryIndex.write failed", error=str(e))

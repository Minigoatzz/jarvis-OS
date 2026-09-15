# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — bootstrap de MEMORY.md (JRV-MEM-001).

Reproduit un bug observé en usage réel : MemoryIndex ne créait ni le
répertoire mémoire ni MEMORY.md lui-même. Agent._build_system() (engine/agent.py)
appelle memory_index.read() à CHAQUE tour de conversation pour construire le
prompt système ; tant qu'aucun pointeur n'avait encore été écrit via
add_pointer() (ConsolidationAgent, en tâche de fond, cadence bien plus lente),
chaque tour déclenchait un FileNotFoundError rattrapé mais logué en WARNING —
confirmé par les logs réels : dizaines d'occurrences de
"Échec ingest mémoire — JRV-MEM-001 (FileNotFoundError ... memory_data\\MEMORY.md)"
dès la première session, avant le premier add_pointer().

Le correctif fait de MemoryIndex.__init__ un bootstrap auto-suffisant, sur le
même modèle que ses classes sœurs du même module (TopicStore, SessionStore),
qui créent déjà leur propre répertoire via mkdir(parents=True, exist_ok=True)
en __init__ — étendu ici à la création d'un MEMORY.md gabarit, puisque
contrairement à un répertoire topics/ ou sessions/ vide (état de démarrage
normal), un MEMORY.md absent fait échouer read() par construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from jarvis.providers.memory.index import MemoryIndex


def _index(memory_dir: Path) -> MemoryIndex:
    from jarvis.providers.memory.index import MemoryIndex

    return MemoryIndex(memory_dir)


# ── Bootstrap au premier lancement ──────────────────────────────────────────


def test_bootstrap_creates_memory_dir_when_missing(tmp_path: Path) -> None:
    """Le répertoire mémoire (et ses parents) n'existe pas encore -> il est créé."""
    memory_dir = tmp_path / "memory_data"
    assert not memory_dir.exists()

    _index(memory_dir)

    assert memory_dir.is_dir()


def test_bootstrap_creates_memory_md_with_template(tmp_path: Path) -> None:
    """MEMORY.md est créé avec un contenu non vide (gabarit), pas un fichier vide."""
    memory_dir = tmp_path / "memory_data"

    idx = _index(memory_dir)

    md_path = memory_dir / "MEMORY.md"
    assert md_path.exists()
    assert md_path.read_text(encoding="utf-8").strip() != ""
    assert idx.read() == md_path.read_text(encoding="utf-8")


def test_bootstrap_read_no_longer_raises_or_warns_on_fresh_start(tmp_path: Path) -> None:
    """Chemin heureux du bug original : read() juste après construction ne doit
    plus jamais passer par le except OSError (donc plus de collector.warning)."""
    memory_dir = tmp_path / "memory_data"

    with patch("jarvis.providers.memory.index.collector") as mock_collector:
        idx = _index(memory_dir)
        content = idx.read()

    mock_collector.warning.assert_not_called()
    assert content != ""


# ── Non-régression : contenu existant jamais écrasé ─────────────────────────


def test_bootstrap_does_not_overwrite_existing_memory_md(tmp_path: Path) -> None:
    """Un MEMORY.md déjà présent (pointeurs réels) doit survivre intact."""
    memory_dir = tmp_path / "memory_data"
    memory_dir.mkdir(parents=True)
    existing = "# MEMORY.md\n\n## Préférences\n- theme: `topics/theme.md` — sombre\n"
    (memory_dir / "MEMORY.md").write_text(existing, encoding="utf-8")

    idx = _index(memory_dir)

    assert idx.read() == existing


def test_bootstrap_does_not_fail_when_dir_already_exists(tmp_path: Path) -> None:
    """Répertoire déjà présent (ex. sessions/ ou topics/ créés en premier) -> pas d'erreur."""
    memory_dir = tmp_path / "memory_data"
    (memory_dir / "sessions").mkdir(parents=True)  # simule SessionStore déjà construit

    _index(memory_dir)  # ne doit pas lever

    assert (memory_dir / "MEMORY.md").exists()


# ── add_pointer fonctionne toujours sur un fichier fraîchement créé ─────────


def test_add_pointer_after_bootstrap_appends_new_section(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory_data"
    idx = _index(memory_dir)

    idx.add_pointer(
        section="Préférences",
        key="theme",
        filepath="topics/theme.md",
        description="thème UI préféré",
    )

    content = idx.read()
    assert "## Préférences" in content
    assert "- theme: `topics/theme.md` — thème UI préféré" in content


# ── Résilience : bootstrap échoue proprement si le répertoire est inaccessible ──


def test_bootstrap_failure_is_caught_and_logged_as_warning(tmp_path: Path) -> None:
    """Si mkdir/write_text échouent au bootstrap (ex. permissions), on logue en
    warning (JRV-MEM-001) sans lever — le comportement dégradé existant de
    read()/_write() reste la garde-fou en dernier recours."""
    memory_dir = tmp_path / "memory_data"

    with patch.object(Path, "mkdir", side_effect=OSError("permission denied")), patch(
        "jarvis.providers.memory.index.collector"
    ) as mock_collector:
        _index(memory_dir)  # ne doit pas lever

    mock_collector.warning.assert_called_once()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

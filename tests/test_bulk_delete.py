# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — effacer en lot : missions échouées, conversations passées.

Jusqu'ici tout se supprimait un par un. workspace/projects accumulait les
tentatives ratées (quatre « Créer fichier bonjour.txt » en deux jours) et les
conversations s'effaçaient fil par fil dans Capacités.

Deux garde-fous portés par ces tests :
  - le ménage des missions ne touche QUE des missions terminées ;
  - la suppression des conversations épargne celle en cours (`keep`), et fait
    le même travail complet que la suppression unitaire (fichier + titre +
    index), pas un simple effacement de fichier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app


class _FakeProject:
    def __init__(self, pid: str, status: str, workspace: Path) -> None:
        self.id, self.status, self.workspace_path = pid, status, str(workspace)


class _FakeOrch:
    def __init__(self, projects: list[_FakeProject]) -> None:
        self._projects = projects
        self.killed: list[str] = []

    def list_projects(self) -> list[_FakeProject]:
        return list(self._projects)

    def get_project(self, pid: str) -> _FakeProject | None:
        return next((p for p in self._projects if p.id == pid), None)

    def kill(self, pid: str) -> bool:
        self.killed.append(pid)
        return True


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def missions(tmp_path: Path):
    made = []
    for pid, status in [
        ("p_fail1", "failed"),
        ("p_fail2", "failed"),
        ("p_run", "running"),
        ("p_done", "done"),
    ]:
        ws = tmp_path / pid
        ws.mkdir()
        (ws / "bonjour.txt").write_text("x", encoding="utf-8")
        made.append(_FakeProject(pid, status, ws))
    orch = _FakeOrch(made)
    previous = getattr(app.state, "orchestrator", None)
    app.state.orchestrator = orch
    yield orch, tmp_path
    app.state.orchestrator = previous


# ── Missions ────────────────────────────────────────────────────────────────


def test_bulk_delete_removes_only_failed_missions(client: TestClient, missions) -> None:
    orch, tmp = missions

    res = client.request("DELETE", "/api/projects", params={"status": "failed"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] == 2
    assert sorted(body["ids"]) == ["p_fail1", "p_fail2"]
    assert not (tmp / "p_fail1").exists() and not (tmp / "p_fail2").exists()
    assert (tmp / "p_run").exists(), "une mission EN VOL ne doit jamais être effacée"
    assert (tmp / "p_done").exists()


def test_bulk_delete_accepts_several_finished_statuses(client: TestClient, missions) -> None:
    _orch, tmp = missions

    res = client.request("DELETE", "/api/projects", params={"status": "failed,done"})

    assert res.json()["deleted"] == 3
    assert (tmp / "p_run").exists()


def test_bulk_delete_refuses_running(client: TestClient, missions) -> None:
    _orch, tmp = missions

    res = client.request("DELETE", "/api/projects", params={"status": "running"})

    assert res.status_code == 400
    assert (tmp / "p_run").exists(), "rien ne doit avoir été touché"


def test_bulk_delete_defaults_to_failed(client: TestClient, missions) -> None:
    res = client.request("DELETE", "/api/projects")
    assert res.json()["deleted"] == 2


# ── Conversations ───────────────────────────────────────────────────────────


@pytest.fixture
def sessions(tmp_path: Path):
    from jarvis.providers.memory.sessions import SessionStore

    directory = tmp_path / "sessions"
    directory.mkdir()
    ids = ["aaa111", "bbb222", "ccc333"]
    for i, sid in enumerate(ids):
        f = directory / f"2026-09-2{i}_{sid}.jsonl"
        f.write_text(json.dumps({"role": "user", "content": "salut"}) + "\n", encoding="utf-8")

    store = SessionStore(directory)
    previous = getattr(app.state, "session_store", None)
    app.state.session_store = store
    yield directory, ids
    app.state.session_store = previous


def test_delete_all_conversations_keeps_the_current_one(client: TestClient, sessions) -> None:
    directory, ids = sessions

    res = client.request("DELETE", "/api/sessions", params={"keep": ids[0]})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] == 2 and body["kept"] == ids[0]
    remaining = sorted(p.stem.split("_", 1)[1] for p in directory.glob("*.jsonl"))
    assert remaining == [ids[0]], "seule la conversation en cours doit rester"


def test_delete_all_conversations_without_keep_removes_everything(
    client: TestClient, sessions
) -> None:
    directory, _ids = sessions

    res = client.request("DELETE", "/api/sessions")

    assert res.json()["deleted"] == 3
    assert list(directory.glob("*.jsonl")) == []


def test_single_delete_still_works(client: TestClient, sessions) -> None:
    """Non-régression : le refactor ne doit pas casser la suppression unitaire."""
    directory, ids = sessions

    res = client.request("DELETE", f"/api/sessions/{ids[1]}")

    assert res.status_code == 200 and res.json()["deleted"] == ids[1]
    remaining = sorted(p.stem.split("_", 1)[1] for p in directory.glob("*.jsonl"))
    assert remaining == sorted([ids[0], ids[2]])


def test_single_delete_unknown_session_is_404(client: TestClient, sessions) -> None:
    assert client.request("DELETE", "/api/sessions/inexistante").status_code == 404


# ── Câblage des boutons ─────────────────────────────────────────────────────

_STATIC = Path(__file__).resolve().parents[1] / "src/jarvis/interfaces/ui/static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_missions_button_targets_only_finished_statuses() -> None:
    js = _read("dashboard.js")
    assert '"/api/projects?status=failed,killed"' in js
    assert "running" not in js.split("api/projects?status=")[1][:80]


def test_conversations_button_keeps_the_current_thread() -> None:
    js = _read("capabilities.js")
    assert '"/api/sessions?keep=" + encodeURIComponent(current.id)' in js
    assert "conversations passées" in js


def test_both_buttons_ask_before_deleting() -> None:
    """Une suppression définitive ne doit jamais partir sur un simple clic."""
    for name in ("dashboard.js", "capabilities.js"):
        js = _read(name)
        section = js[js.index("api/projects?status=") - 900 :] if name == "dashboard.js" else js[js.index("api/sessions?keep=") - 900 :]
        assert "confirm(" in section, f"{name} : pas de confirmation avant suppression"

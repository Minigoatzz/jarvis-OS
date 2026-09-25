# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Lancement de sous-processus independant de la boucle asyncio.

Sur Windows, uvicorn choisit une SelectorEventLoop des que reload=True — et
cette boucle ne sait pas lancer de sous-processus : `create_subprocess_shell`
y leve NotImplementedError. Comme `str(NotImplementedError())` vaut la chaine
vide, l'interface affichait « Erreur outil execute_cli: » suivi de RIEN.
Toute l'execution (chat et missions) etait morte, en silence.

Ces tests tournent sous Linux, ou la SelectorEventLoop supporte les
sous-processus. On simule donc la panne Windows en rendant l'API de la boucle
indisponible : le code corrige ne doit pas la toucher du tout.
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from jarvis.engine.mission.backends.local import LocalBackend
from jarvis.kernel.subprocess_compat import describe_exception, run_exec, run_shell


@pytest.fixture
def _boucle_sans_sous_processus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduit la SelectorEventLoop de Windows : API sous-processus absente."""

    async def _indisponible(*args: object, **kwargs: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _indisponible)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _indisponible)


# ── Le message d'erreur ne peut plus etre vide ──────────────────────────────


def test_une_exception_sans_message_garde_un_nom() -> None:
    """La cause du silence : NotImplementedError n'a pas de message."""
    assert str(NotImplementedError()) == ""
    assert describe_exception(NotImplementedError()) == "NotImplementedError"


def test_un_message_existant_est_conserve() -> None:
    assert describe_exception(ValueError("chemin introuvable")) == "chemin introuvable"


# ── Le runner ne depend plus de la boucle ───────────────────────────────────


def test_run_shell_marche_sans_api_sous_processus(_boucle_sans_sous_processus: None) -> None:
    res = asyncio.run(run_shell("echo bonjour"))
    assert res.success
    assert res.stdout.strip() == "bonjour"


def test_run_exec_marche_sans_api_sous_processus(_boucle_sans_sous_processus: None) -> None:
    res = asyncio.run(run_exec(["echo", "bonjour"]))
    assert res.success
    assert res.stdout.strip() == "bonjour"


def test_un_code_retour_non_nul_est_un_echec_pas_une_exception() -> None:
    res = asyncio.run(run_shell("exit 3"))
    assert not res.success
    assert res.returncode == 3


def test_le_timeout_leve_bien_TimeoutExpired() -> None:
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        asyncio.run(run_shell("sleep 5", timeout=1))


# ── Le backend des missions survit a la boucle Windows ──────────────────────


def test_local_backend_execute_sur_une_boucle_sans_sous_processus(
    _boucle_sans_sous_processus: None,
) -> None:
    """Le test qui compte : c'est exactement la panne de production.

    L'ancien code appelait asyncio.create_subprocess_shell, attrapait
    NotImplementedError dans son `except Exception`, et renvoyait
    stderr=str(exc) — donc stderr vide, succes False, aucune explication.
    """
    workspace = tempfile.mkdtemp()
    res = asyncio.run(LocalBackend(workspace).execute("echo bonjour", timeout=10))

    assert res["success"], f"stderr={res['stderr']!r}"
    assert "bonjour" in res["stdout"]


def test_le_backend_execute_bien_dans_le_workspace(_boucle_sans_sous_processus: None) -> None:
    import pathlib

    workspace = tempfile.mkdtemp()
    pathlib.Path(workspace, "temoin.txt").write_text("ici", encoding="utf-8")
    res = asyncio.run(LocalBackend(workspace).execute("cat temoin.txt", timeout=10))
    assert res["stdout"].strip() == "ici"


def test_une_erreur_du_backend_n_est_jamais_muette(monkeypatch: pytest.MonkeyPatch) -> None:
    """« Erreur outil execute_cli: » suivi de rien ne doit plus exister."""
    import jarvis.engine.mission.backends.local as module

    async def _boum(*args: object, **kwargs: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(module, "run_shell", _boum)
    res = asyncio.run(LocalBackend(tempfile.mkdtemp()).execute("echo x", timeout=5))

    assert not res["success"]
    assert res["stderr"].strip(), "un echec sans message est un echec invisible"
    assert res["stderr"] == "NotImplementedError"


# ── Le bac a sable du chat est conscient de la plateforme ───────────────────


def test_sandbox_env_posix_reste_restreint(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.capabilities.tools import cli

    monkeypatch.setattr(cli.os, "name", "posix")
    env = cli._sandbox_env("/tmp/bac")
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    assert env["HOME"] == "/tmp/bac"


def test_sandbox_env_windows_garde_de_quoi_resoudre_un_binaire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un PATH POSIX sur Windows ne resout AUCUN binaire : rien ne demarrait."""
    from jarvis.capabilities.tools import cli

    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(
        cli.os, "environ", {"PATH": r"C:\Windows\system32", "SYSTEMROOT": r"C:\Windows"}
    )
    env = cli._sandbox_env(r"C:\bac")

    assert env["PATH"] == r"C:\Windows\system32"
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["TEMP"] == r"C:\bac"
    assert env["USERPROFILE"] == r"C:\bac"


def test_le_registre_d_outils_n_affiche_jamais_une_erreur_vide() -> None:
    """La phrase exacte de la capture : « Erreur outil execute_cli: » puis rien."""
    import asyncio as _aio

    from jarvis.capabilities.tools.registry import ToolRegistry

    class _Casse:
        name = "execute_cli"
        description = "outil de test"
        input_schema: dict = {"type": "object", "properties": {}}

        async def execute(self, **_: object) -> None:
            raise NotImplementedError

    registre = ToolRegistry()
    registre.register(_Casse())
    res = _aio.run(registre.call("execute_cli", {}))

    assert res.is_error
    assert "NotImplementedError" in res.content
    assert not res.content.rstrip().endswith("execute_cli:")

# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Verifier : un controle objectif par etape, une recette sur preuves a la fin.

Remplace la verification en trois couches par etape. Voir le module pour
l'historique ; ici, chaque decision de conception a son test.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from jarvis.engine.mission.quality_checker import QualityChecker
from jarvis.engine.mission.verifier import Verifier
from jarvis.kernel.schemas import LogEntry, Project, Step, StepStatus


class _Juge:
    """LLM factice : rend un verdict donne et garde le dernier prompt recu."""

    def __init__(self, reponse: object = None, erreur: Exception | None = None) -> None:
        self._reponse = reponse
        self._erreur = erreur
        self.prompt = ""
        self.appels = 0

    async def complete(self, messages: list[dict], system: str, **_: object) -> object:
        self.appels += 1
        self.prompt = messages[-1]["content"]
        if self._erreur is not None:
            raise self._erreur
        return self._reponse if isinstance(self._reponse, str) else json.dumps(self._reponse)

    async def health_check(self) -> bool:
        return True


def _verifier(ws: Path, juge: _Juge | None = None) -> Verifier:
    return Verifier(QualityChecker(str(ws)), juge or _Juge())  # type: ignore[arg-type]


def _projet(ws: Path, mission: str = "m") -> Project:
    etape = Step("s1", "Écrire", "d", status=StepStatus.DONE, output="fait")
    return Project(id="p", title="t", mission=mission, steps=[etape], workspace_path=str(ws))


def _journal(*messages: str, **data: object) -> list[LogEntry]:
    return [LogEntry(datetime.now(), "tool", m, data=data or None) for m in messages]


# ── 1. Par etape : seulement l'objectif ─────────────────────────────────────


def test_une_etape_sans_defaut_passe_meme_sans_rien_changer(tmp_path: Path) -> None:
    """proj_b2ca0d : l'etape etait recalee pour « Aucun fichier nouveau ou modifie »
    alors que la date etait deja dans le fichier."""
    (tmp_path / "bonjour.txt").write_text("2026-09-23\n", encoding="utf-8")
    assert _verifier(tmp_path).check_step().verified


def test_un_fichier_vide_est_un_defaut(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    verdict = _verifier(tmp_path).check_step()
    assert not verdict.verified
    assert any("vide" in i for i in verdict.issues)


def test_un_init_py_vide_n_est_pas_un_defaut(tmp_path: Path) -> None:
    """Sinon toute mission qui cree un paquet Python echoue."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    assert _verifier(tmp_path).check_step().verified


def test_un_fichier_existant_reecrit_avec_une_erreur_est_vu(tmp_path: Path) -> None:
    """L'ancien controle ne regardait que les fichiers NOUVEAUX."""
    script = tmp_path / "script.py"
    script.write_text("print(1)\n", encoding="utf-8")
    verifier = _verifier(tmp_path)
    assert verifier.check_step().verified
    script.write_text("print(\n", encoding="utf-8")  # meme chemin, contenu casse
    assert not verifier.check_step().verified


def test_les_references_html_ne_bloquent_pas_une_etape(tmp_path: Path) -> None:
    """index.html est ecrit AVANT style.css dans un plan normal."""
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="style.css">', encoding="utf-8"
    )
    assert _verifier(tmp_path).check_step().verified


def test_l_etape_n_appelle_jamais_le_llm(tmp_path: Path) -> None:
    juge = _Juge()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _verifier(tmp_path, juge).check_step()
    assert juge.appels == 0


# ── 2. La recette voit TOUTES les preuves ───────────────────────────────────


def test_la_recette_voit_le_contenu_reel_des_fichiers(tmp_path: Path) -> None:
    """proj_d57ef2 : le worker avait ecrit « $(date +%Y-%m-%d) » litteralement.
    Le juge doit voir ce contenu pour pouvoir le refuser."""
    (tmp_path / "bonjour.txt").write_text("$(date +%Y-%m-%d)", encoding="utf-8")
    juge = _Juge({"accepted": False, "missing": ["date"], "reason": "r"})
    asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), []))
    assert "$(date +%Y-%m-%d)" in juge.prompt


def test_la_recette_voit_une_suppression(tmp_path: Path) -> None:
    """proj_e7b565 : l'ancien juge ne voyait que les fichiers nouveaux, une
    suppression reussie etait indiscernable d'un oubli."""
    (tmp_path / "notes1.txt").write_text("a", encoding="utf-8")
    juge = _Juge({"accepted": True, "missing": [], "reason": "ok"})
    journal = _journal("delete_file: notes2.txt")
    asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), journal))
    assert "delete_file: notes2.txt" in juge.prompt
    assert "=== notes2.txt" not in juge.prompt


def test_la_recette_voit_la_sortie_d_une_execution(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("print([2, 3, 5])\n", encoding="utf-8")
    juge = _Juge({"accepted": True, "missing": [], "reason": "ok"})
    journal = _journal("execute_cli: python3 script.py", returncode=0, output="[2, 3, 5]")
    asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), journal))
    assert "code 0" in juge.prompt and "[2, 3, 5]" in juge.prompt


def test_la_recette_juge_la_demande_d_origine(tmp_path: Path) -> None:
    juge = _Juge({"accepted": True, "missing": [], "reason": "ok"})
    projet = _projet(tmp_path, mission="calcule les 100 premiers nombres premiers")
    asyncio.run(_verifier(tmp_path, juge).accept(projet, []))
    assert "calcule les 100 premiers nombres premiers" in juge.prompt
    assert "Sans preuve d'un manque, accepte" in juge.prompt


# ── 3. Le verdict ───────────────────────────────────────────────────────────


def test_un_verdict_positif_est_accepte(tmp_path: Path) -> None:
    juge = _Juge({"accepted": True, "missing": [], "reason": "demande satisfaite"})
    verdict = asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), []))
    assert verdict.accepted and verdict.judged


def test_un_refus_porte_ses_manques(tmp_path: Path) -> None:
    juge = _Juge({"accepted": False, "missing": ["semaine.md absent"], "reason": "incomplet"})
    verdict = asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), []))
    assert not verdict.accepted and verdict.judged
    assert verdict.missing == ["semaine.md absent"]


def test_seul_le_booleen_true_vaut_acceptation(tmp_path: Path) -> None:
    for valeur in ("true", "oui", 1, None):
        juge = _Juge({"accepted": valeur, "missing": [], "reason": "r"})
        verdict = asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), []))
        assert not verdict.accepted, valeur


def test_une_cloture_markdown_autour_du_json_est_toleree(tmp_path: Path) -> None:
    juge = _Juge('```json\n{"accepted": true, "missing": [], "reason": "ok"}\n```')
    assert asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), [])).accepted


def test_un_juge_en_panne_n_est_pas_un_refus_argumente(tmp_path: Path) -> None:
    """Rien de precis a corriger : pas de ronde de correction (judged=False)."""
    juge = _Juge(erreur=ConnectionError("Ollama injoignable"))
    verdict = asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), []))
    assert not verdict.accepted and not verdict.judged
    assert "Ollama injoignable" in verdict.reason


def test_une_reponse_illisible_a_une_seconde_chance(tmp_path: Path) -> None:
    juge = _Juge("je pense que oui")
    verdict = asyncio.run(_verifier(tmp_path, juge).accept(_projet(tmp_path), []))
    assert juge.appels == 2
    assert not verdict.accepted and not verdict.judged

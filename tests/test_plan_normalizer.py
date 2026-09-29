# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Frontiere planificateur -> moteur : plan_normalizer.

Chaque cas ci-dessous a ete DEMONTRE en echec sur le code precedent, qui ne
faisait que `json.loads` puis des acces `step["id"]` en dur.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.engine.mission.plan_normalizer import (
    PlanError,
    as_access_level,
    as_bool,
    extract_plan_json,
    is_generated_report_step,
    normalize_plan,
    renumber_steps,
)
from jarvis.engine.mission.project_manager import ProjectManager
from jarvis.kernel.schemas import Project
from jarvis.kernel.vocab import AUTO_MAX_LEVEL, AccessLevel


def _step(**kw: object) -> dict:
    base: dict = {"id": "step_001", "title": "Écrire", "description": "d",
                  "success_criterion": "test -s a.txt"}
    base.update(kw)
    return base


# ── Extraction ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        '{"steps": []}',
        'Voici le plan :\n{"steps": []}\nBonne journée',
        '```json\n{"steps": []}\n```',
        '<think>je réfléchis {pas du json}</think>\n{"steps": []}',
    ],
)
def test_le_plan_est_extrait_malgre_l_enrobage(raw: str) -> None:
    assert extract_plan_json(raw) == {"steps": []}


def test_une_sortie_sans_plan_donne_un_message_lisible() -> None:
    with pytest.raises(PlanError, match="plan lisible"):
        extract_plan_json("Désolé, je ne peux pas.")


# ── Coercitions ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [(True, True), ("false", False), ("False", False), ("oui", True),
     ("true", True), (0, False), (1, True), (None, False), ("n'importe", False)],
)
def test_la_chaine_false_est_fausse(valeur: object, attendu: bool) -> None:
    """bool("false") vaut True : une etape demandait une approbation pour rien."""
    assert as_bool(valeur) is attendu


def test_un_niveau_absent_prend_le_defaut_documente() -> None:
    assert as_access_level(None) is AccessLevel.WRITE_LOCAL


@pytest.mark.parametrize("valeur", ["NETWORK", "network", 3, "3"])
def test_un_niveau_lisible_est_respecte(valeur: object) -> None:
    assert as_access_level(valeur) is AccessLevel.NETWORK


@pytest.mark.parametrize("valeur", ["élevé", True, [1], {"a": 1}])
def test_un_niveau_illisible_n_est_jamais_auto_execute(valeur: object) -> None:
    """Le piege : « invalide -> defaut WRITE_LOCAL » aurait auto-execute
    ce que le modele avait peut-etre marque comme risque."""
    assert as_access_level(valeur) > AUTO_MAX_LEVEL


@pytest.mark.parametrize(("valeur", "attendu"), [(9, AccessLevel.MODIFY_CORE),
                                                  (2.1, AccessLevel.NETWORK),
                                                  (-3, AccessLevel.READ_ONLY)])
def test_un_nombre_hors_bornes_n_abaisse_jamais_le_controle(
    valeur: float, attendu: AccessLevel
) -> None:
    """int(2.7) tronque vers 2 : on arrondit vers le haut."""
    assert as_access_level(valeur) is attendu


# ── Normalisation ───────────────────────────────────────────────────────────


def test_un_titre_absent_est_deduit_de_la_mission() -> None:
    plan = normalize_plan({"steps": [_step()]}, mission="crée un fichier bonjour.txt")
    assert plan["title"] == "crée un fichier bonjour.txt"


def test_un_critere_null_devient_vide_sans_etre_invente() -> None:
    """Le plan sera refuse par l'orchestrateur (contrat §4.2) — avec un message clair,
    au lieu d'un AttributeError sur None.strip()."""
    plan = normalize_plan({"steps": [_step(success_criterion=None)]}, mission="m")
    assert plan["steps"][0]["success_criterion"] == ""


def test_une_etape_sans_titre_ni_description_est_ecartee() -> None:
    plan = normalize_plan({"steps": [_step(), {"id": "x"}, "pas un dict"]}, mission="m")
    assert len(plan["steps"]) == 1


def test_un_plan_sans_etape_exploitable_est_refuse_lisiblement() -> None:
    with pytest.raises(PlanError, match="aucune étape"):
        normalize_plan({"steps": [{"id": "x"}]}, mission="m")
    with pytest.raises(PlanError, match="liste d'étapes"):
        normalize_plan({"steps": "étape 1"}, mission="m")


def test_la_renumerotation_supprime_les_doublons() -> None:
    steps = renumber_steps([_step(id="step_003"), _step(id="step_003"), _step(id="")])
    assert [s["id"] for s in steps] == ["step_001", "step_002", "step_003"]


# ── L'etape RAPPORT.md du moteur, et elle seule ─────────────────────────────


def test_seule_l_etape_rapport_md_est_reconnue_comme_generee() -> None:
    assert is_generated_report_step(_step(title="Générer RAPPORT.md"))
    assert not is_generated_report_step(_step(title="Rédiger le rapport hebdomadaire"))
    assert not is_generated_report_step(_step(title="Relire le rapport"))


# ── Integration : le vrai planificateur ─────────────────────────────────────


def _planifier(plan: dict | str, mission: str = "m") -> Project:
    raw = plan if isinstance(plan, str) else json.dumps(plan)
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=raw)
    store = MagicMock()
    store.create_project = lambda mission, title, timeout_minutes: Project(
        id="p", title=title, mission=mission
    )
    pm = ProjectManager.__new__(ProjectManager)
    pm._llm = llm
    pm._store = store
    return asyncio.run(pm.create_project(mission))


def test_une_mission_de_rapport_garde_ses_etapes_de_redaction() -> None:
    """L'ancien filtre retirait toute etape contenant « rapport » : la mission
    « redige un rapport sur ma semaine » n'ecrivait jamais le rapport."""
    projet = _planifier(
        {"title": "Rapport", "project_type": "content", "steps": [
            _step(title="Collecter les événements"),
            _step(id="step_002", title="Rédiger le rapport hebdomadaire"),
            _step(id="step_003", title="Relire le rapport"),
        ]},
        mission="rédige un rapport sur ma semaine",
    )
    titres = [s.title for s in projet.steps]
    assert "Rédiger le rapport hebdomadaire" in titres
    assert "Relire le rapport" in titres


def test_les_etapes_finales_ont_des_ids_uniques() -> None:
    projet = _planifier({"title": "T", "steps": [_step(id="step_003"), _step(id="step_001")]})
    ids = [s.id for s in projet.steps]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "etape",
    [
        {k: v for k, v in _step().items() if k != "id"},
        _step(access_level="WRITE_LOCAL"),
        _step(access_level=9),
        _step(success_criterion=None),
    ],
)
def test_le_planificateur_ne_plante_plus_sur_une_etape_mal_formee(etape: dict) -> None:
    projet = _planifier({"title": "T", "steps": [etape]})
    assert projet.steps


def test_le_planificateur_accepte_de_la_prose_autour_du_json() -> None:
    projet = _planifier('Voici le plan :\n' + json.dumps({"title": "T", "steps": [_step()]}))
    assert projet.title == "T"


# ── Une verification qui ne peut pas echouer n'est pas une verification ─────


@pytest.mark.parametrize(
    "commande",
    [
        "test -f notes2.txt && echo 'existe' || echo 'supprimé'",  # proj_e7b565
        "test -s out.txt || true",
        "grep -q x f.txt; echo fini",
        "echo ok",
        "python3 s.py; exit 0",
    ],
)
def test_une_tautologie_est_retiree_du_plan(commande: str) -> None:
    from jarvis.engine.mission.plan_normalizer import is_tautological

    assert is_tautological(commande)
    plan = normalize_plan({"steps": [_step(verification_command=commande)]}, mission="m")
    assert plan["steps"][0]["verification_command"] is None


@pytest.mark.parametrize(
    "commande",
    [
        "test -f notes1.txt && grep -c '^' notes1.txt",
        "test -s RAPPORT.md && grep -c '^## ' RAPPORT.md | awk '{exit ($1 >= 3) ? 0 : 1}'",
        "test -f index.html && echo trouve",
        "! test -f notes2.txt",
    ],
)
def test_une_vraie_verification_est_conservee(commande: str) -> None:
    """Le filtre ne doit pas toucher la verification RAPPORT.md injectee par le moteur."""
    plan = normalize_plan({"steps": [_step(verification_command=commande)]}, mission="m")
    assert plan["steps"][0]["verification_command"] == commande


def test_le_prompt_du_planificateur_porte_les_contre_exemples_du_jour() -> None:
    from jarvis.engine.mission.project_manager import _PLANNING_SYSTEM as prompt

    assert "UNE étape qui l'écrit EN ENTIER" in prompt
    assert "RÉSULTAT OBSERVABLE" in prompt
    assert "! test -f notes2.txt" in prompt
    assert "delete_file" in prompt

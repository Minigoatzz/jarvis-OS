# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Transforme la sortie brute du planificateur en plan sur a executer.

Le planificateur est un LLM. Sa sortie est une suggestion, pas un contrat :
ce module est la frontiere entre les deux. Tout ce qui arrive ensuite dans le
moteur (identifiants, niveaux d'acces, booleens) a ete verifie ici.

Fonctions pures, sans LLM ni I/O : elles se testent seules et s'etendent sans
toucher au moteur. Pour accepter une nouvelle forme de sortie du modele, c'est
ici.

Defauts corriges — tous DEMONTRES sur le code precedent, qui ne faisait que
`json.loads` puis des acces `step["id"]` en dur :

- « Voici le plan : {...} » -> toute la mission echouait au parsing ;
- une cle absente -> KeyError, annonce « Je n'ai pas pu planifier : 'id' » ;
- `success_criterion: null` -> AttributeError sur None.strip() ;
- `access_level: "WRITE_LOCAL"` ou `9` -> ValueError ;
- `requires_approval: "false"` -> stocke tel quel, la CHAINE "false" est vraie :
  approbation demandee pour rien ;
- identifiants dupliques acceptes, alors que reclamations et verification
  fonctionnent par identifiant.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from jarvis.kernel.vocab import AUTO_MAX_LEVEL, AccessLevel

_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Une valeur d'acces ILLISIBLE ne doit jamais s'executer sans humain : elle prend
# le premier niveau qui exige une validation. Derive de la constante, pas code en
# dur — si AUTO_MAX_LEVEL change, cette regle suit.
_UNREADABLE_ACCESS = AccessLevel(min(int(AUTO_MAX_LEVEL) + 1, max(AccessLevel)))

_TRUE = {"true", "vrai", "oui", "yes", "1"}


class PlanError(ValueError):
    """Plan inexploitable. Le message est destine a l'utilisateur, tel quel."""


# ── Extraction ───────────────────────────────────────────────────────────────


def extract_plan_json(raw: str) -> dict[str, Any]:
    """Extrait l'objet JSON du plan, meme entoure de prose ou de raisonnement."""
    text = _THINK_RE.sub("", raw or "")
    text = _FENCE_RE.sub("", text).strip()

    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            # jrv: pas de code — on essaie le candidat suivant ; l'echec final leve PlanError
            continue
        if isinstance(data, dict):
            return data
    raise PlanError("le planificateur n'a pas renvoyé de plan lisible")


# ── Coercitions ──────────────────────────────────────────────────────────────


def as_bool(value: object) -> bool:
    """Booleen tolerant : la chaine "false" est FAUSSE (bool("false") vaut True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return False


def as_access_level(value: object) -> AccessLevel:
    """Niveau d'acces sans jamais abaisser le controle demande.

    - absent            -> WRITE_LOCAL (le defaut documente dans le prompt) ;
    - nombre            -> ramene dans l'echelle [READ_ONLY, MODIFY_CORE] ;
    - nom (« NETWORK ») -> ce niveau ;
    - illisible         -> premier niveau soumis a validation humaine.
    """
    if value is None or value == "":
        return AccessLevel.WRITE_LOCAL
    if isinstance(value, str):
        name = value.strip().upper()
        if name in AccessLevel.__members__:
            return AccessLevel[name]
        if not re.fullmatch(r"-?\d+", value.strip()):
            return _UNREADABLE_ACCESS
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _UNREADABLE_ACCESS
    # ceil et non int() : int(2.7) tronque vers 2 et ABAISSERAIT le contrôle.
    level = math.ceil(value)
    clamped = max(int(min(AccessLevel)), min(level, int(max(AccessLevel))))
    return AccessLevel(clamped)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# ── Normalisation ────────────────────────────────────────────────────────────


def normalize_plan(data: dict[str, Any], *, mission: str) -> dict[str, Any]:
    """Plan de forme garantie : titre, liste d'etapes, champs types.

    Ne fabrique JAMAIS de critere de succes : une etape sans critere garde un
    critere vide, et la validation de l'orchestrateur refuse le plan — contrat
    PHASE 1 §4.2, inchange.
    """
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise PlanError("le plan ne contient pas de liste d'étapes")

    steps: list[dict[str, Any]] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title")) or _text(item.get("description"))
        if not title:
            continue  # une etape sans titre ni description ne decrit rien
        verification = _text(item.get("verification_command"))
        steps.append(
            {
                "id": _text(item.get("id")),
                "title": title,
                "description": _text(item.get("description")) or title,
                "success_criterion": _text(item.get("success_criterion")),
                "verification_command": verification or None,
                "requires_approval": as_bool(item.get("requires_approval")),
                "access_level": int(as_access_level(item.get("access_level"))),
            }
        )

    if not steps:
        raise PlanError("le plan ne contient aucune étape exploitable")

    return {
        "title": _text(data.get("title")) or (mission.strip()[:40] or "Mission"),
        "project_type": _text(data.get("project_type")) or "generic",
        "requires_network": as_bool(data.get("requires_network")),
        "steps": steps,
    }


def is_generated_report_step(step: dict[str, Any]) -> bool:
    """Vrai pour l'etape RAPPORT.md que le moteur ajoute lui-meme.

    L'ancien filtre retirait toute etape dont le TITRE contenait « rapport » :
    pour « redige un rapport sur ma semaine », l'etape qui ecrivait le rapport
    demande etait supprimee. On ne retire que ce qui cible le fichier RAPPORT.md.
    """
    haystack = " ".join(
        str(step.get(k) or "")
        for k in ("title", "description", "success_criterion", "verification_command")
    )
    return "rapport.md" in haystack.lower()


def renumber_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identifiants step_001..step_N, dans l'ordre d'execution.

    Les identifiants du modele ne sont pas fiables (doublons, trous) et ceux des
    etapes ajoutees par le moteur etaient calcules sur le NOMBRE d'etapes : des
    collisions apparaissaient. Aucun identifiant n'est encore reference a ce
    stade — le projet n'existe pas — donc renumeroter est sans risque.
    """
    for index, step in enumerate(steps, start=1):
        step["id"] = f"step_{index:03d}"
    return steps

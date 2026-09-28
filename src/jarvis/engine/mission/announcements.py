# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Ce que Jarvis DIT a l'utilisateur au sujet d'une mission.

Un seul fichier pour toute la formulation : lancement, creation, plan
invalide, fin. Aucune I/O ici — des fonctions pures, testables, qu'on modifie
sans toucher au moteur. Pour changer un message, c'est ici et nulle part
ailleurs.

Pourquoi ce module existe
-------------------------
Le 28/09, la mission proj_6cf963 a parfaitement reussi — 3 etapes sur 3
verifiees, bonjour.txt contenait la bonne date — et l'utilisateur a conclu
qu'aucune mission n'avait ete creee. Deux raisons, toutes deux dans la
communication, aucune dans le moteur :

- le chat affichait la synthese d'un outil DU CHAT (execute_script) qui
  n'aurait jamais du tourner en route mission : « Fait. Le fichier a ete cree » ;
- la page d'accueil n'ecoutait aucun evenement de mission. Seul le dashboard
  savait qu'une mission existait.

Le moteur marchait. Personne ne le disait.

Canal
-----
Les annonces passent par l'evenement `{"type": "message", "role": "assistant"}`
que `home.js` affiche deja via `showChannel()` (le canal du moteur proactif).
Aucun code frontend supplementaire. `showChannel()` tronque a 160 caracteres :
chaque annonce est bornee par `CHANNEL_LIMIT`.
"""

from __future__ import annotations

from typing import Any

from jarvis.kernel.schemas import Project, ProjectStatus, StepStatus

# showChannel() (home.js) tronque au-dela : on borne nous-memes, proprement.
CHANNEL_LIMIT = 160


def _clip(text: str, limit: int = CHANNEL_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _label(project: Project) -> str:
    """« titre » (id) — le titre parle a l'utilisateur, l'id sert a retrouver."""
    title = (project.title or "").strip() or "sans titre"
    return f"« {title} » ({project.id})"


# ── Moments du cycle de vie ──────────────────────────────────────────────────


def launch_ack() -> str:
    """Reponse IMMEDIATE du chat quand la route est PROJECT.

    Deterministe par choix : le premier jet du modele ne peut pas etre cru a
    ce moment-la. Quand la route a ete forcee, il a ecrit une reponse [I]
    (« Fait. ») alors que rien n'etait encore fait. La mission n'existe meme
    pas encore — le planificateur tourne apres l'envoi de `done`.
    """
    return (
        "Mission lancée — je planifie les étapes. "
        "Je te préviens ici dès qu'elle est prête, et tu la suis dans Missions."
    )


def created(project: Project) -> str:
    n = len(project.steps)
    etapes = f"{n} étape" + ("s" if n > 1 else "")
    return _clip(f"Mission {_label(project)} créée : {etapes}, exécution en cours.")


def plan_invalid(error: str) -> str:
    return _clip(f"Je n'ai pas pu planifier la mission : {error}")


def finished(project: Project, reason: str | None = None) -> str:
    """Annonce terminale — succes, echec ou arret. Toujours une cause en cas d'echec.

    `reason` couvre les echecs qui ne tiennent a aucune etape : exception
    imprevue, etape restee PENDING. L'etape en echec reste prioritaire, parce
    qu'elle dit OU la mission s'est arretee.
    """
    if project.status is ProjectStatus.DONE:
        n = len(project.files_created)
        fichiers = f"{n} fichier" + ("s" if n > 1 else "")
        return _clip(f"Mission {_label(project)} terminée : {fichiers} produit(s).")

    if project.status is ProjectStatus.KILLED:
        return _clip(f"Mission {_label(project)} arrêtée.")

    # FAILED (ou tout autre etat terminal inattendu) : nommer l'etape et la cause.
    echec = next((s for s in project.steps if s.status is StepStatus.FAILED), None)
    if echec is not None:
        cause = (echec.error or "cause inconnue").strip()
        return _clip(f"Mission {_label(project)} échouée à « {echec.title} » : {cause}")
    if reason:
        return _clip(f"Mission {_label(project)} échouée : {reason.strip()}")
    return _clip(f"Mission {_label(project)} échouée.")


# ── Enveloppe d'evenement ────────────────────────────────────────────────────


def chat_message(text: str) -> dict[str, Any]:
    """Evenement WebSocket que home.js affiche deja (canal passif)."""
    return {"type": "message", "role": "assistant", "text": text}

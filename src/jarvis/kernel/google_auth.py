# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Identifiants Google — UN seul chargeur, jamais interactif.

Il en existait trois copies (outil Gmail, outil Calendar, collecteur d'emails).
Sans token, chacune lançait la connexion interactive d'`InstalledAppFlow` : une
connexion, depuis le SERVEUR, que personne ne voit. Le rappel de
calendrier tourne toutes les minutes : chaque passage aurait gelé un thread à
attendre un navigateur. La connexion passe par un seul chemin : la page
Capacités (interfaces/api/google_oauth.py).

Les permissions demandées sont aussi une frontière de sécurité :
- `gmail.modify` : lire, classer, archiver, étiqueter, mettre à la corbeille.
  PAS la suppression définitive — elle exige `https://mail.google.com/`, que
  Jarvis ne demande pas. « Supprimer » veut donc toujours dire « corbeille »,
  récupérable 30 jours, même si le modèle se trompe.
- `gmail.send` : envoyer (chaque envoi te demande ton accord).
- Aucun `gmail.settings.*` : ni filtre ni transfert automatique. Une règle de
  transfert est « réversible », mais elle enverrait en silence tout ton courrier
  futur à quelqu'un d'autre. Impossible par construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.kernel.error_collector import collector  # jrv: autofix

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleNotConnected(FileNotFoundError):  # noqa: N818 — un état, pas un bug
    """Le service n'est pas connecté (token absent, expiré ou révoqué).

    Hérite de FileNotFoundError : les appelants traitent déjà ce cas comme
    « intégration non configurée » (niveau warning, backoff du scheduler).
    """


def load_google_credentials(token_path: Path, service: str) -> Credentials:
    """Charge le token de `service`, le rafraîchit au besoin. Bloquant : à appeler
    dans un thread. Lève GoogleNotConnected au lieu d'ouvrir une connexion."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    reconnect = f"reconnecte {service} depuis la page Capacités de Jarvis."
    if not token_path.exists():
        raise GoogleNotConnected(f"{service} n'est pas connecté : {reconnect}")

    # Sans `scopes=` : on garde ceux réellement accordés, inscrits dans le token.
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise GoogleNotConnected(f"La connexion {service} a expiré : {reconnect}")
    try:
        creds.refresh(Request())
    except RefreshError as e:
        # Token révoqué ou expiré côté Google : seule une reconnexion le répare.
        # Une panne réseau (TransportError) n'arrive pas ici : elle remonte telle
        # quelle, et le token reste en place pour le prochain essai.
        collector.warning("JRV-TOL-015", "JRV-TOL-015", cause=e)
        raise GoogleNotConnected(f"La connexion {service} a été refusée : {reconnect}") from e
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds

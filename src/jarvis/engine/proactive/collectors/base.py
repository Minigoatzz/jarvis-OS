# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

from abc import ABC, abstractmethod

from loguru import logger

from jarvis.engine.proactive.schemas import ContextItem
from jarvis.kernel.connectivity import is_offline_mode
from jarvis.kernel.error_collector import collector  # jrv: autofix


class SourceUnavailable(Exception):  # noqa: N818 — un état, pas une erreur de code
    """La source n'a pas pu être lue (non configurée, hors ligne, en erreur).

    À lever au lieu de `return []` : une liste vide veut dire « rien à signaler »,
    et le résumé en tirait « Agenda libre » quand l'agenda n'avait jamais été lu.
    """


class CollectorBase(ABC):
    name: str = "base"
    # Raison pour laquelle la DERNIÈRE collecte n'a pas pu lire la source ;
    # None si elle l'a lue (même vide). ContextBuilder s'en sert pour ne pas
    # confondre « rien » et « inconnu ».
    last_error: str | None = None

    async def collect(self) -> list[ContextItem]:
        """Point d'entrée principal. Ne lève jamais : rend [] et note `last_error`."""
        self.last_error = None
        try:
            items = await self._collect()
            logger.debug(f"Collector {self.name}: {len(items)} items")
            return items
        except SourceUnavailable as e:
            # jrv: source non configurée ou hors ligne — un état attendu, pas un
            # incident. Volontairement non mappé (scripts/error_audit/scan.py).
            self.last_error = str(e) or "source indisponible"
            logger.debug(f"Collector {self.name} indisponible : {self.last_error}")
            return []
        except Exception as e:
            collector.warning("JRV-PRO-001", "JRV-PRO-001", cause=e)
            self.last_error = str(e) or type(e).__name__
            if is_offline_mode():
                logger.debug(f"Collector {self.name} ignoré — mode local ({type(e).__name__})")
            else:
                logger.error(f"Collector {self.name} failed: {e}")
            return []

    @abstractmethod
    async def _collect(self) -> list[ContextItem]:
        """Implémenter dans chaque sous-classe."""

# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""
EmailCollector — récupère les emails non lus avec le corps complet du thread.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime

import httpx

from jarvis.engine.proactive.collectors.base import CollectorBase, SourceUnavailable
from jarvis.engine.proactive.schemas import ContextItem, ItemType, Priority
from jarvis.kernel.connectivity import is_offline_mode
from jarvis.kernel.error_collector import collector  # jrv: autofix
from jarvis.kernel.google_auth import load_google_credentials
from jarvis.kernel.paths import PROJECT_ROOT
from jarvis.kernel.settings import settings

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

LOW_PRIORITY_PATTERNS = [
    "noreply",
    "no-reply",
    "newsletter",
    "notification",
    "donotreply",
    "unsubscribe",
    "automated",
]

HIGH_SUBJECT_KEYWORDS = [
    "urgent",
    "asap",
    "important",
    "deadline",
    "payment",
    "invoice",
    "facture",
    "contrat",
    "partenariat",
]

HIGH_SENDER_DOMAINS = ["@anthropic", "@pcbway", "@nextpcb"]


def _extract_text_body(payload: dict) -> str:
    """Extraction récursive du texte plain depuis un payload MIME Gmail."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            try:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:
                collector.warning("JRV-PRO-001", "JRV-PRO-001")
                return ""
    for part in payload.get("parts", []):
        result = _extract_text_body(part)
        if result:
            return result
    return ""


class EmailCollector(CollectorBase):
    name = "email"

    async def _collect(self) -> list[ContextItem]:
        if is_offline_mode():
            raise SourceUnavailable("mode local")

        token_path = PROJECT_ROOT / settings.google_gmail_token_path
        try:
            creds = await asyncio.to_thread(load_google_credentials, token_path, "Gmail")
        except FileNotFoundError as e:
            collector.warning("JRV-PRO-001", "JRV-PRO-001", cause=e)
            raise SourceUnavailable(str(e)) from e

        return await self._fetch_messages(creds.token)

    async def _fetch_messages(self, access_token: str) -> list[ContextItem]:
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(
                f"{_GMAIL_BASE}/messages",
                headers=headers,
                # UNREAD + INBOX : restreint aux non-lus de la boîte de réception
                # (exclut archivés/Promotions/Social), cohérent avec le tool gmail.
                params={"labelIds": ["UNREAD", "INBOX"], "maxResults": 15},
            )
            r.raise_for_status()
            messages = r.json().get("messages", [])
            if not messages:
                return []

            async def fetch_full(msg_id: str) -> dict:
                resp = await client.get(
                    f"{_GMAIL_BASE}/messages/{msg_id}",
                    headers=headers,
                    params={"format": "full"},
                )
                resp.raise_for_status()
                return resp.json()

            metas = await asyncio.gather(*[fetch_full(m["id"]) for m in messages])

        items = []
        for msg in metas:
            payload = msg.get("payload", {})
            hdrs = {h["name"]: h["value"] for h in payload.get("headers", [])}
            sender = hdrs.get("From", "")
            subject = hdrs.get("Subject", "Sans sujet")
            date = hdrs.get("Date", "")
            msg_id = hdrs.get("Message-ID", "")
            thread_id = msg.get("threadId", "")
            snippet = msg.get("snippet", "")

            # Corps complet du message (limité pour le contexte)
            body = _extract_text_body(payload)[:1500]

            sender_lower = sender.lower()
            if any(p in sender_lower for p in LOW_PRIORITY_PATTERNS):
                continue

            priority = self._assess_priority(subject, sender_lower)

            raw = (
                f"De: {sender}\nSujet: {subject}\nDate: {date}\n"
                f"Message-ID: {msg_id}\nThread-ID: {thread_id}\n\n"
                f"{body or snippet}"
            )

            items.append(
                ContextItem(
                    type=ItemType.EMAIL,
                    title=subject,
                    summary=snippet[:200],
                    raw=raw,
                    source="gmail",
                    timestamp=datetime.now(),
                    priority=priority,
                    metadata={
                        "from": sender,
                        "date": date,
                        "id": msg.get("id", ""),
                        "thread_id": thread_id,
                        "message_id": msg_id,
                    },
                )
            )

        return items

    def _assess_priority(self, subject: str, sender_lower: str) -> Priority:
        if any(kw in subject.lower() for kw in HIGH_SUBJECT_KEYWORDS):
            return Priority.HIGH
        if any(d in sender_lower for d in HIGH_SENDER_DOMAINS):
            return Priority.HIGH
        return Priority.MEDIUM

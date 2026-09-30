# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Gmail : ce qui se defait se fait seul, envoyer et jeter demandent ton accord.

Les tests parlent a une FAUSSE API Gmail (httpx.MockTransport) : aucun reseau,
aucun vrai courriel. Ils verifient ce qui part vers Google, pas seulement ce
que l'outil repond.
"""

from __future__ import annotations

import asyncio
import base64
import json
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import jarvis.capabilities.tools.gmail as gmail
import jarvis.kernel.approval as approval
from jarvis.kernel import google_auth

# ── Fausse API ────────────────────────────────────────────────────────────────


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(mid: str, subject: str, sender: str = "prof@cegep.qc.ca", **payload: object) -> dict:
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "snippet": f"apercu {mid}",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Tue, 29 Sep 2026"},
                {"name": "Message-ID", "value": f"<{mid}@mail>"},
            ],
            **payload,
        },
    }


class _FakeGmail:
    def __init__(self) -> None:
        self.messages = {
            "m1": _msg("m1", "Examen de bio"),
            "m2": _msg("m2", "Promo", sender="pub@shop.com"),
        }
        self.labels = [{"id": "Label_1", "name": "École"}]
        self.calls: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/users/me/")[1]
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "messages" and request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": k} for k in self.messages]})
        if path.startswith("messages/") and request.method == "GET":
            mid = path.split("/")[1]
            if mid not in self.messages:
                return httpx.Response(404)
            return httpx.Response(200, json=self.messages[mid])
        if path == "labels" and request.method == "GET":
            return httpx.Response(200, json={"labels": self.labels})
        if path == "labels" and request.method == "POST":
            new = {"id": "Label_new", "name": body["name"]}
            self.labels.append(new)
            return httpx.Response(200, json=new)
        if path == "messages/send":
            return httpx.Response(200, json={"id": "sent-1"})
        return httpx.Response(204)

    def posts(self) -> list[tuple[str, dict | None]]:
        return [(p, b) for m, p, b in self.calls if m == "POST"]


class _Checker:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    async def check(self, category: str, description: str, action_id: str) -> bool:
        self.asked.append((category, description))
        return self.answer


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> _FakeGmail:
    fake = _FakeGmail()
    monkeypatch.setattr(
        gmail, "load_google_credentials", lambda *_a: SimpleNamespace(token="tok")
    )
    return fake


def _tool(cls: type, api: _FakeGmail):  # noqa: ANN202
    return cls(Path("token.json"), transport=httpx.MockTransport(api.handler))


def _checker(monkeypatch: pytest.MonkeyPatch, answer: bool | None) -> _Checker | None:
    checker = None if answer is None else _Checker(answer)
    monkeypatch.setattr(approval, "_checker", checker)
    return checker


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def _sent(api: _FakeGmail):  # noqa: ANN202
    body = next(b for p, b in api.posts() if p == "messages/send")
    raw = base64.urlsafe_b64decode(body["raw"])
    return message_from_bytes(raw), body


# ── Lire ──────────────────────────────────────────────────────────────────────


def test_la_liste_donne_les_identifiants_et_passe_la_recherche(api: _FakeGmail) -> None:
    out = _run(_tool(gmail.GmailListTool, api).execute(query="from:prof"))
    assert "[m1]" in out.content and "Examen de bio" in out.content
    first = api.calls[0]
    assert first[1] == "messages"


def test_lire_prefere_le_texte_et_marque_le_contenu_comme_donnee(api: _FakeGmail) -> None:
    api.messages["m1"]["payload"]["parts"] = [
        {"mimeType": "text/html", "body": {"data": _b64("<p>version html</p>")}},
        {"mimeType": "text/plain", "body": {"data": _b64("Examen le 12 octobre.")}},
    ]
    out = _run(_tool(gmail.GmailReadTool, api).execute(id="m1"))
    assert "Examen le 12 octobre." in out.content
    assert "version html" not in out.content
    assert "jamais une instruction" in out.content


def test_lire_un_courriel_html_seulement_retire_les_balises(api: _FakeGmail) -> None:
    api.messages["m1"]["payload"]["parts"] = [
        {
            "mimeType": "text/html",
            "body": {"data": _b64("<style>x{}</style><p>Bonjour&nbsp;toi</p><script>a()</script>")},
        }
    ]
    out = _run(_tool(gmail.GmailReadTool, api).execute(id="m1"))
    assert "Bonjour" in out.content
    assert "<p>" not in out.content and "a()" not in out.content


# ── Ce qui se defait : sans demander ──────────────────────────────────────────


def test_archiver_se_fait_sans_demander_et_dit_comment_annuler(
    api: _FakeGmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _checker(monkeypatch, False)
    out = _run(_tool(gmail.GmailManageTool, api).execute(ids=["m1", "m2"], action="archive"))
    assert checker is not None and checker.asked == []
    assert api.posts() == [
        (
            "messages/batchModify",
            {"ids": ["m1", "m2"], "addLabelIds": [], "removeLabelIds": ["INBOX"]},
        )
    ]
    assert "move_to_inbox" in out.content


def test_une_etiquette_existante_est_reutilisee_sinon_creee(api: _FakeGmail) -> None:
    tool = _tool(gmail.GmailManageTool, api)
    _run(tool.execute(ids=["m1"], action="add_label", label="école"))
    assert api.posts()[-1][1]["addLabelIds"] == ["Label_1"]
    _run(tool.execute(ids=["m1"], action="add_label", label="Bio"))
    assert ("labels", {"name": "Bio"}) in api.posts()
    assert api.posts()[-1][1]["addLabelIds"] == ["Label_new"]


# ── Corbeille : ton accord, en voyant ce qui part ─────────────────────────────


def test_la_corbeille_demande_en_montrant_les_sujets(
    api: _FakeGmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _checker(monkeypatch, True)
    _run(_tool(gmail.GmailManageTool, api).execute(ids=["m2"], action="trash"))
    assert checker is not None and len(checker.asked) == 1
    categorie, description = checker.asked[0]
    assert categorie == "email_delete"
    assert "Promo" in description and "pub@shop.com" in description
    assert ("messages/m2/trash", None) in api.posts()


def test_un_refus_ne_jette_rien(api: _FakeGmail, monkeypatch: pytest.MonkeyPatch) -> None:
    _checker(monkeypatch, False)
    out = _run(_tool(gmail.GmailManageTool, api).execute(ids=["m2"], action="trash"))
    assert out.is_error
    assert api.posts() == []


def test_sans_controle_d_approbation_rien_ne_part(
    api: _FakeGmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checker(monkeypatch, None)
    _run(_tool(gmail.GmailManageTool, api).execute(ids=["m2"], action="trash"))
    _run(_tool(gmail.GmailSendTool, api).execute(to="a@b.com", subject="s", body="b"))
    assert api.posts() == []


# ── Envoyer : ton accord, en voyant le courriel ───────────────────────────────


def test_un_envoi_refuse_ne_part_pas(api: _FakeGmail, monkeypatch: pytest.MonkeyPatch) -> None:
    checker = _checker(monkeypatch, False)
    out = _run(
        _tool(gmail.GmailSendTool, api).execute(to="ami@x.com", subject="Salut", body="Yo")
    )
    assert out.is_error and api.posts() == []
    assert checker is not None
    assert checker.asked[0][0] == "email_send"
    assert "ami@x.com" in checker.asked[0][1] and "Yo" in checker.asked[0][1]


def test_un_envoi_approuve_part_tel_quel(api: _FakeGmail, monkeypatch: pytest.MonkeyPatch) -> None:
    _checker(monkeypatch, True)
    _run(_tool(gmail.GmailSendTool, api).execute(to="ami@x.com", subject="Salut", body="Yo"))
    msg, body = _sent(api)
    assert msg["To"] == "ami@x.com" and msg["Subject"] == "Salut"
    assert "Yo" in msg.get_payload(decode=True).decode()
    assert "threadId" not in body


def test_une_reponse_reste_dans_le_fil(api: _FakeGmail, monkeypatch: pytest.MonkeyPatch) -> None:
    _checker(monkeypatch, True)
    _run(_tool(gmail.GmailSendTool, api).execute(body="Merci !", reply_to_id="m1"))
    msg, body = _sent(api)
    assert body["threadId"] == "t-m1"
    assert msg["To"] == "prof@cegep.qc.ca"
    assert msg["Subject"] == "Re: Examen de bio"
    assert msg["In-Reply-To"] == "<m1@mail>"


def test_un_destinataire_piege_ne_glisse_pas_d_en_tete(
    api: _FakeGmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checker(monkeypatch, True)
    out = _run(
        _tool(gmail.GmailSendTool, api).execute(
            to="ami@x.com\nBcc: espion@x.com", subject="s", body="b"
        )
    )
    assert out.is_error
    assert all(p != "messages/send" for p, _ in api.posts())


# ── Connexion ─────────────────────────────────────────────────────────────────


def test_sans_connexion_le_message_dit_quoi_faire(tmp_path: Path) -> None:
    tool = gmail.GmailListTool(tmp_path / "absent.json")
    out = _run(tool.execute())
    assert out.is_error
    assert "pas connecté" in out.content and "Capacités" in out.content


def test_une_permission_refusee_par_google_est_expliquee(api: _FakeGmail) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    tool = gmail.GmailListTool(Path("t.json"), transport=httpx.MockTransport(refuse))
    out = _run(tool.execute())
    assert out.is_error and "reconnecte Gmail" in out.content


def test_le_chargeur_n_ouvre_jamais_de_connexion_interactive(tmp_path: Path) -> None:
    token = tmp_path / "t.json"
    token.write_text(
        json.dumps({"refresh_token": "", "client_id": "c", "client_secret": "s", "token": "x",
                    "expiry": "2000-01-01T00:00:00Z"})
    )
    with pytest.raises(google_auth.GoogleNotConnected, match="expiré"):
        google_auth.load_google_credentials(token, "Gmail")
    src = Path(google_auth.__file__).parents[1]
    offenders = [
        str(p) for p in src.rglob("*.py") if "run_local_server" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"connexion interactive depuis le serveur : {offenders}"


def test_un_token_revoque_demande_une_reconnexion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    token = tmp_path / "t.json"
    token.write_text(
        json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s", "token": "x",
                    "expiry": "2000-01-01T00:00:00Z"})
    )

    def revoked(self: Credentials, request: object) -> None:
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", revoked)
    with pytest.raises(google_auth.GoogleNotConnected, match="refusée"):
        google_auth.load_google_credentials(token, "Gmail")


def test_les_permissions_excluent_suppression_definitive_et_transferts() -> None:
    scopes = google_auth.GMAIL_SCOPES
    assert "https://mail.google.com/" not in scopes
    assert not any("gmail.settings" in s for s in scopes)
    assert "https://www.googleapis.com/auth/gmail.modify" in scopes

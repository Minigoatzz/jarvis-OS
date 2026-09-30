# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Un resultat d'outil n'est pas une parole de l'utilisateur.

Incident du 30/09 : « montre-moi mon dernier email » a affiche le resultat brut
de list_emails, suivi de « /no_think » ; « montre mes courriels non lus » a
affiche « list_emails(action="list", …) ». L'outil avait marche les deux fois.
La synthese passait le resultat au modele comme un message UTILISATEUR, et le
modele le recopiait. Ces tests verifient ce qui part vers Ollama.
"""

from __future__ import annotations

from jarvis.providers.llm.local import OllamaProvider, _to_ollama_messages

_EMAIL = "[1a0f] De : Google\nSujet : Security alert"


def _synthese() -> list[dict]:
    """Les messages exacts qu'Agent.synthesize() construit."""
    return [
        {"role": "user", "content": "montre moi mon dernier email"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "list_emails", "input": {"max_results": 1}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": _EMAIL}],
        },
    ]


def test_le_resultat_part_en_message_tool_et_jamais_en_parole_utilisateur() -> None:
    payload = OllamaProvider()._payload(_synthese(), "système", stream=True)
    messages = payload["messages"]

    assert messages[-1] == {"role": "tool", "content": _EMAIL}
    paroles = [m["content"] for m in messages if m["role"] == "user"]
    assert paroles == ["montre moi mon dernier email"]
    assert not any("Security alert" in p for p in paroles)


def test_l_appel_d_outil_part_en_tool_calls_natifs() -> None:
    assistant = OllamaProvider()._payload(_synthese(), "s", stream=False)["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"] == [
        {"function": {"name": "list_emails", "arguments": {"max_results": 1}}}
    ]
    assert "list_emails" not in assistant["content"], "aucune notation d'appel a recopier"


def test_plusieurs_resultats_gardent_leur_ordre() -> None:
    message = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "un"},
            {"type": "tool_result", "tool_use_id": "b", "content": "deux"},
        ],
    }
    assert _to_ollama_messages(message) == [
        {"role": "tool", "content": "un"},
        {"role": "tool", "content": "deux"},
    ]


def test_un_message_texte_ordinaire_ne_change_pas() -> None:
    assert _to_ollama_messages({"role": "user", "content": "salut"}) == [
        {"role": "user", "content": "salut"}
    ]
    blocs = {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
    assert _to_ollama_messages(blocs) == [{"role": "assistant", "content": "ok"}]

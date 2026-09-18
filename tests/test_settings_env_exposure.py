# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — cohérence entre les clés éditables dans l'UI et leur application.

Deux invariants, tous deux issus de pannes réelles.

1. MAPBOX_TOKEN n'existait que dans `.env.example`. Le globe ne démarre pas sans
   lui, mais aucun écran ne permettait de le saisir : il fallait éditer `.env` à
   la main et redémarrer. `GET /api/settings` construit `api_keys` à partir de
   `_SENSITIVE_KEYS`, et la section « Clés API » de settings.js boucle sur ce
   dictionnaire — y inscrire la clé suffit donc à la rendre visible ET éditable.

2. Une clé éditable dans l'UI doit soit être appliquée à chaud
   (`_SETTINGS_FIELD_MAP`), soit annoncer un redémarrage (`_RESTART_KEYS`).
   Quatre d'entre elles n'étaient ni l'un ni l'autre : `/api/settings/update`
   écrivait `.env`, l'objet `settings` vivant gardait l'ancienne valeur, et la
   réponse ne signalait aucun redémarrage. Sauvegarde silencieusement sans effet.
"""

from __future__ import annotations

import pytest

from jarvis.interfaces.api.config._env import (
    _RESTART_KEYS,
    _SENSITIVE_KEYS,
    _SETTINGS_FIELD_MAP,
)
from jarvis.kernel.settings import Settings


def test_map_keys_are_editable_from_the_ui() -> None:
    """Sans ça, le globe exige une édition manuelle de .env."""
    assert "MAPBOX_TOKEN" in _SENSITIVE_KEYS
    assert "MAPTILER_KEY" in _SENSITIVE_KEYS


def test_map_keys_apply_without_a_restart() -> None:
    """/api/globe/config relit settings à chaque chargement de la vue."""
    assert _SETTINGS_FIELD_MAP.get("MAPBOX_TOKEN") == "mapbox_token"
    assert _SETTINGS_FIELD_MAP.get("MAPTILER_KEY") == "maptiler_key"


@pytest.mark.parametrize("env_key", sorted(_SETTINGS_FIELD_MAP))
def test_every_mapped_key_points_at_a_real_settings_field(env_key: str) -> None:
    """Un champ inexistant fait échouer le hot-apply en silence (hasattr faux)."""
    field = _SETTINGS_FIELD_MAP[env_key]

    assert field in Settings.model_fields, (
        f"{env_key} pointe vers le champ inexistant '{field}' — "
        "le hot-apply sera ignoré sans erreur."
    )


def test_no_editable_key_saves_without_taking_effect() -> None:
    """L'invariant de fond : éditable ⇒ appliquée à chaud OU redémarrage annoncé.

    Sont exclues les clés consommées uniquement via os.getenv (LiveKit, bots) :
    `/api/settings/update` met à jour os.environ, donc elles prennent effet sans
    passer par l'objet settings.
    """
    fields = Settings.model_fields

    silent = sorted(
        key
        for key in _SENSITIVE_KEYS
        if key not in _SETTINGS_FIELD_MAP
        and key not in _RESTART_KEYS
        and key.lower() in fields
    )

    assert not silent, (
        f"Clés éditables dans l'UI mais sans effet sur le processus vivant : {silent}. "
        "Ajoute-les à _SETTINGS_FIELD_MAP (hot-apply) ou à _RESTART_KEYS "
        "(l'UI annoncera alors un redémarrage)."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
from loguru import logger

from jarvis.kernel.error_collector import collector  # jrv: autofix
from jarvis.kernel.schemas import ToolCapture
from jarvis.kernel.settings import settings
from jarvis.providers.llm.base import LLMProvider

# Strip <think>...</think> au cas où Ollama les laisse passer (fallback)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_MAX_TOOL_ITERATIONS = 8


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).lstrip()


def _flatten_content(content: object) -> str:
    """Aplatit un contenu en blocs (format Anthropic) en texte pour Ollama.

    Agent.synthesize() construit le second appel au format Anthropic : `content`
    est une LISTE de blocs (`text`, `tool_use`, `tool_result`). L'API Ollama
    exige une CHAÎNE et répond 400 Bad Request sur une liste. Ce chemin n'avait
    jamais été exercé en local — aucun outil ne s'y exécutait — donc le 400
    n'est apparu qu'une fois les outils enfin fonctionnels : l'outil agissait,
    puis la synthèse plantait et l'utilisateur voyait « j'ai eu un souci ».

    Les blocs sont rendus en texte étiqueté pour que le modèle garde
    l'information dont il a besoin afin de rédiger sa réponse finale.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            args = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[outil appelé] {block.get('name', '?')}({args})")
        elif btype == "tool_result":
            parts.append(f"[résultat outil] {block.get('content', '')}")
        elif "text" in block:
            parts.append(str(block["text"]))
    return "\n".join(p for p in parts if p)


def _normalize_message(message: dict) -> dict:
    """Garantit un `content` de type str — seul format accepté par /api/chat."""
    content = message.get("content")
    if isinstance(content, str):
        return message
    return {**message, "content": _flatten_content(content)}


def _parse_ollama_tool_calls(
    raw_tool_calls: list[dict], id_hint: str
) -> list[tuple[str, str, dict]]:
    """Normalise les tool_calls Ollama en (id, nom, arguments).

    Ollama n'émet pas toujours d'`id`, et `arguments` est tantôt un dict, tantôt
    une chaîne JSON selon le modèle — les deux sont gérés. Partagé par tool_loop
    (non streaming) et _stream (capture streaming) pour que les deux chemins
    interprètent identiquement une même réponse.
    """
    parsed: list[tuple[str, str, dict]] = []
    for i, tc in enumerate(raw_tool_calls):
        fn = tc.get("function", {})
        name: str = fn.get("name", "")
        raw_args = fn.get("arguments", {})
        call_id: str = tc.get("id") or f"call_{name}_{id_hint}_{i}"

        if isinstance(raw_args, str):
            try:
                args: dict = json.loads(raw_args)
            except json.JSONDecodeError:
                collector.error("JRV-LLM-002", "JRV-LLM-002")
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}

        parsed.append((call_id, name, args))
    return parsed


def _claude_tools_to_ollama(tools: list[dict]) -> list[dict]:
    """Convertit le schéma d'outils interne Jarvis (format Claude) vers le format Ollama/OpenAI.

    Entrée  : [{"name": "...", "description": "...", "input_schema": {...}}]
    Sortie  : [{"type": "function", "function": {"name", "description", "parameters"}}]
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


class OllamaProvider(LLMProvider):
    """Provider Ollama pour les modèles locaux (Qwen2.5/3, Llama 3.1+, Mistral…).

    supports_tools retourne True : Ollama accepte le champ "tools" pour les modèles
    compatibles (Qwen2.5/3, Llama 3.1+, Mistral…). Les modèles non-tool ignorent ce
    champ silencieusement — tool_loop retourne alors le texte brut sans exécuter d'outil.
    """

    def __init__(self) -> None:
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._model = settings.ollama_model

    @property
    def supports_tools(self) -> bool:
        """True — Ollama route le champ "tools" vers les modèles compatibles.

        Avertissement : un modèle non-tool (ex. petit Qwen3) ignorera les outils et
        ne produira jamais de tool_calls. tool_loop terminera normalement mais sans
        avoir exécuté d'outil — le résultat sera incomplet si une action était attendue.
        """
        return True

    def _payload(
        self,
        messages: list[dict],
        system: str,
        stream: bool,
        tools: list[dict] | None = None,
    ) -> dict:
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                *(_normalize_message(m) for m in messages),
            ],
            "stream": stream,
            "think": False,  # désactive le mode reasoning Qwen3 côté Ollama
            "options": {"temperature": 0.7, "num_ctx": settings.ollama_num_ctx},
        }
        if tools:
            payload["tools"] = _claude_tools_to_ollama(tools)
        return payload

    async def _post_chat(self, client: httpx.AsyncClient, payload: dict) -> httpx.Response:
        """POST /api/chat avec repli automatique sur le champ "think".

        Certaines versions d'Ollama (ou certains modèles ne déclarant pas la
        capability "thinking") répondent 400 dès que le payload contient la
        clé "think" — c'est la cause confirmée de JRV-LLM-002 observée en
        usage réel. On retente une seule fois sans ce champ avant d'abandonner,
        et on journalise systématiquement le corps d'erreur d'Ollama (jusque-là
        silencieusement perdu par raise_for_status()) pour permettre un vrai
        diagnostic la prochaine fois.

        Ne lève jamais elle-même : comme avant, c'est à l'appelant d'appeler
        response.raise_for_status() sur la réponse retournée (celle du retry
        en cas de repli). On détecte l'erreur ici via raise_for_status() dans
        un try/except plutôt qu'en inspectant response.status_code directement,
        pour rester compatible avec les mocks httpx existants de la suite de
        tests (qui ne configurent que raise_for_status, pas status_code).
        """
        response = await client.post(f"{self._base_url}/api/chat", json=payload)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body_text = exc.response.text
            if (
                exc.response.status_code == 400
                and "think" in payload
                and "think" in body_text.lower()
            ):
                logger.warning(
                    "Ollama a refusé le champ 'think' — retry sans",
                    model=self._model,
                    ollama_error=body_text[:300],
                )
                fallback = {k: v for k, v in payload.items() if k != "think"}
                return await client.post(f"{self._base_url}/api/chat", json=fallback)

            # Corps inliné dans le message : le format loguru du projet n'affiche
            # pas les kwargs, et l'erreur réelle d'Ollama y était donc invisible.
            logger.error(
                f"Ollama /api/chat error — status={exc.response.status_code} "
                f"model={self._model} body={body_text[:500]}"
            )

        return response

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        stream: bool = False,
        context: str = "",
    ) -> str | AsyncIterator[str]:
        payload = self._payload(messages, system, stream, tools)

        if stream:
            return self._stream(payload)

        # read=300 pour absorber le cold-start du modèle (chargement GPU/CPU ~100s+)
        _timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=5.0)
        async with httpx.AsyncClient(timeout=_timeout) as client:
            response = await self._post_chat(client, payload)
            response.raise_for_status()
            data = response.json()
            text: str = data["message"]["content"]
            logger.debug("Ollama complete", model=self._model, chars=len(text))
            return _strip_think(text)

    def stream_with_capture(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> tuple[AsyncIterator[str], ToolCapture]:
        """Stream le texte ET capture les tool_calls — contrat commun aux providers.

        Sans cette méthode, Agent.start_routing_stream() retombait sur sa branche
        « provider sans outil », sélectionnée par un simple
        `hasattr(self._llm, "stream_with_capture")`. Cette branche appelle
        complete(stream=True) SANS transmettre `tools` : le payload envoyé à
        Ollama ne contenait donc aucun schéma d'outil, et la capture rendue au
        gateway valait None. Le modèle ne connaissait les outils que par leur
        description en toutes lettres dans le prompt système (les skills-vues y
        documentent `show_view(action="show", view_id="clock")`) et recopiait
        cette notation en texte : « spotify_control(action="pause") » s'affichait
        dans la conversation et rien ne s'exécutait. Observé en usage réel — en
        mode local, AUCUN outil ne pouvait s'exécuter en conversation normale,
        alors que tool_loop() ci-dessous implémentait déjà l'appel natif.
        """
        capture = ToolCapture()
        payload = self._payload(messages, system, stream=True, tools=tools)
        return self._stream(payload, capture), capture

    async def _stream(
        self, payload: dict, capture: ToolCapture | None = None
    ) -> AsyncIterator[str]:
        """Comme _post_chat, mais pour le mode streaming.

        httpx.AsyncClient.stream() ne peut pas être rejoué via un simple
        retry de la réponse (le corps n'est lu qu'à la demande) : on construit
        donc une liste de payloads candidats (avec puis, si refusé, sans le
        champ "think") et on ouvre un nouveau flux HTTP pour chaque candidat,
        en s'arrêtant dès que l'un réussit. Un seul retry maximum — pas de
        boucle infinie possible car `candidates` a une longueur fixe (1 ou 2).
        """
        _timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=5.0)
        candidates: list[dict] = [payload]
        if "think" in payload:
            candidates.append({k: v for k, v in payload.items() if k != "think"})

        async with httpx.AsyncClient(timeout=_timeout) as client:
            for attempt, candidate in enumerate(candidates):
                is_last = attempt == len(candidates) - 1
                async with client.stream(
                    "POST", f"{self._base_url}/api/chat", json=candidate
                ) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        await resp.aread()
                        body_text = resp.text
                        if not is_last and exc.response.status_code == 400 and (
                            "think" in body_text.lower()
                        ):
                            logger.warning(
                                "Ollama a refusé le champ 'think' (stream) — retry sans",
                                model=self._model,
                                ollama_error=body_text[:300],
                            )
                            continue
                        logger.error(
                            f"Ollama /api/chat error (stream) — "
                            f"status={exc.response.status_code} model={self._model} "
                            f"body={body_text[:500]}"
                        )
                        raise

                    in_think = False
                    think_buf = ""

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        message: dict = data.get("message", {})

                        # Ollama émet les tool_calls complets dans un chunk (pas
                        # de JSON partiel token par token comme Anthropic) : on
                        # les capture au passage sans interrompre le flux texte.
                        if capture is not None:
                            raw_calls: list[dict] = message.get("tool_calls") or []
                            if raw_calls:
                                capture.calls.extend(
                                    _parse_ollama_tool_calls(
                                        raw_calls, id_hint=f"stream{len(capture.calls)}"
                                    )
                                )
                                capture.stop_reason = "tool_use"

                        delta: str = message.get("content", "")

                        if delta:
                            # Filtre <think>...</think> token par token (sécurité)
                            think_buf += delta
                            output = ""
                            while think_buf:
                                if in_think:
                                    end = think_buf.find("</think>")
                                    if end == -1:
                                        think_buf = ""
                                        break
                                    think_buf = think_buf[end + len("</think>") :]
                                    in_think = False
                                else:
                                    start = think_buf.find("<think>")
                                    if start == -1:
                                        output += think_buf
                                        think_buf = ""
                                        break
                                    output += think_buf[:start]
                                    think_buf = think_buf[start + len("<think>") :]
                                    in_think = True
                            if output:
                                yield output

                        if data.get("done"):
                            break
                    return

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        context: str = "",
    ) -> str:
        """Boucle tool use Ollama (function calling natif, /api/chat).

        Envoie le champ "tools" et traite message.tool_calls en multi-tours.
        Robustesse :
        - arguments dict ou string JSON selon le modèle — les deux sont gérés.
        - outil inconnu ou erreur d'exécution → tool result d'erreur renvoyé au modèle
          plutôt qu'une exception, pour permettre l'auto-correction.
        - arrêt automatique après _MAX_TOOL_ITERATIONS tours pour éviter les boucles.
        """
        tool_names = {t["name"] for t in tools}
        ollama_tools = _claude_tools_to_ollama(tools)
        current: list[dict] = [
            {"role": "system", "content": system},
            *(_normalize_message(m) for m in messages),
        ]

        async def _exec_one(call_id: str, name: str, args: dict) -> tuple[str, str]:
            if name not in tool_names:
                logger.warning("Ollama tool_loop: outil inconnu", name=name)
                return call_id, f"Erreur : outil '{name}' inconnu."
            try:
                result = await tool_executor(name, args)
                return call_id, result
            except Exception as exc:
                collector.error("JRV-LLM-002", "JRV-LLM-002", cause=exc)
                logger.warning("Ollama tool_loop: erreur exécution", name=name, error=str(exc))
                return call_id, f"Erreur lors de l'exécution de '{name}' : {exc}"

        for iteration in range(_MAX_TOOL_ITERATIONS):
            payload: dict = {
                "model": self._model,
                "messages": current,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.7, "num_ctx": settings.ollama_num_ctx},
                "tools": ollama_tools,
            }

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await self._post_chat(client, payload)
                response.raise_for_status()
                data = response.json()

            msg: dict = data.get("message", {})
            raw_tool_calls: list[dict] = msg.get("tool_calls") or []

            if not raw_tool_calls:
                text: str = _strip_think(msg.get("content", ""))
                logger.debug("Ollama tool loop done", iterations=iteration + 1)
                return text

            # Réinjecte la réponse assistant avec ses tool_calls dans l'historique
            current.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": raw_tool_calls,
                }
            )

            # Parse les tool calls : arguments peuvent être dict OU string JSON
            parsed = _parse_ollama_tool_calls(raw_tool_calls, id_hint=str(iteration))

            results: list[tuple[str, str]] = await asyncio.gather(
                *(_exec_one(cid, n, a) for cid, n, a in parsed)
            )
            logger.debug("Ollama tools called", names=[n for _, n, _ in parsed])

            for _cid, result in results:
                current.append({"role": "tool", "content": result})

        logger.warning("Ollama tool loop max iterations reached", max=_MAX_TOOL_ITERATIONS)
        return "Je n'ai pas pu terminer — trop d'étapes."

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                return response.status_code == 200
        except Exception as e:
            collector.error("JRV-LLM-002", "JRV-LLM-002", cause=e)
            logger.error("Ollama health check failed", error=str(e))
            return False

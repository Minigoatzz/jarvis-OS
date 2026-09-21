# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from loguru import logger

from jarvis.engine.agent import (
    Agent,
    all_tools_failed_message,
    claims_completion,
    is_degenerate_reply,
)
from jarvis.engine.background.notifications import NotificationQueue
from jarvis.engine.background.worker import BackgroundWorker
from jarvis.engine.llm_errors import friendly_llm_error
from jarvis.engine.router import RouteEnum, SpeedRouter
from jarvis.engine.session import Session, SessionManager
from jarvis.kernel.contracts import CrossSessionRecall
from jarvis.kernel.error_collector import collector  # jrv: autofix


def _fallback(exc: BaseException | None = None) -> str:
    if exc is not None:
        return friendly_llm_error(exc)
    return friendly_llm_error(RuntimeError("unknown"))


class Gateway:
    """Point d'entrée unique. Gère session, notifications, routing et agent.

    Phase C : le constructeur Gateway était DÉJÀ bien injecté en pré-C
    (5 dépendances reçues par paramètres typés). Le singleton historique
    `_tool_registry_instance` a été supprimé à l'étape 2 (b) — les call-sites
    (preset, http_skills) reçoivent maintenant le ToolRegistry via constructeur
    ou `request.app.state.container.tool_registry`.

    Flux double-passe pour les outils (CF) :
    1. Premier appel LLM streamé : détection du tag + ack text + capture tool_use.
    2. Exécution parallèle des outils (overlap avec TTS de l'ack).
    3. Second appel LLM (synthesize) : résultats injectés dans le contexte,
       LLM produit une réponse naturelle — pas de dump brut.
    L'utilisateur reçoit : ack streamé → synthèse streamée dans la même bulle.
    [BG] : le worker est soumis par le WebSocket après "done".
    """

    def __init__(
        self,
        session_manager: SessionManager,
        agent: Agent,
        notifications: NotificationQueue,
        worker: BackgroundWorker,
        recall: CrossSessionRecall | None = None,
    ) -> None:
        self._sessions = session_manager
        self._agent = agent
        self._notifications = notifications
        self._worker = worker
        self._recall = recall

    async def handle(
        self,
        message: str,
        session_id: str | None = None,
        stream: bool = True,
    ) -> tuple[Session, RouteEnum, str | AsyncIterator[str]]:
        session = self._sessions.get_or_create(session_id)
        logger.info("Gateway handle", session_id=str(session.id))

        pending = self._notifications.drain()
        notif_texts = [n.content for n in pending] if pending else None
        if notif_texts:
            logger.info("Injecting notifications", count=len(notif_texts))

        # Rappel cross-session uniquement au premier message de la session
        recall_summary: str | None = None
        if self._recall is not None and not session.messages:
            try:
                recall_summary = await self._recall.recall(message)
                if recall_summary:
                    logger.debug("CrossSessionRecall injected", chars=len(recall_summary))
            except Exception as e:
                collector.error("JRV-GWY-001", "JRV-GWY-001", cause=e)
                logger.warning("CrossSessionRecall failed", error=str(e))

        try:
            raw_stream, tool_capture = self._agent.start_routing_stream(
                session=session,
                user_message=message,
                notifications=notif_texts,
                recall_summary=recall_summary,
            )

            route, text_stream = await SpeedRouter.extract_route(raw_stream)
            logger.debug("Route detected", route=route.value)

            agent = self._agent
            notifications = self._notifications

            async def _pipe() -> AsyncIterator[str]:
                tool_task: asyncio.Task | None = None
                ack_text = ""  # Accumule le texte streamé avant les outils
                held: list[str] = []
                emitted = False

                # L'AFFICHAGE du premier jet est retenu jusqu'à savoir si des
                # outils vont tourner. Deux symptômes réels, tous deux constatés
                # avec qwen3:14b :
                #   - le modèle répond avant l'exécution ("C'est fait.") et la
                #     synthèse le redit après : la réponse s'affichait en double,
                #     parfois en quadruple ;
                #   - il écrit l'appel en toutes lettres, et cette ligne partait
                #     telle quelle dans la conversation.
                #
                # La condition a d'abord été limitée à la route CF. C'était faux :
                # le modèle écrit l'appel ET tague [I] (« joue holyman par blind
                # melon » -> [I] + spotify_control(...)). Le tag est une intention
                # déclarée par le modèle, pas un fait ; on ne peut pas s'en servir
                # pour décider s'il y aura un outil. On retient donc dès que le
                # provider peut capturer des outils, et on libère le texte si
                # aucun n'a tourné.
                #
                # Le stream reste CONSOMMÉ au fil de l'eau : la task outil démarre
                # aussi tôt qu'avant, seul l'affichage est différé — d'au plus la
                # durée du premier jet, qu'on attendait déjà avant la synthèse.
                defer = tool_capture is not None

                async for chunk in text_stream:
                    ack_text += chunk
                    if defer:
                        held.append(chunk)
                    else:
                        emitted = True
                        yield chunk
                    # Dès que _stream_capturing peuple capture (content_block_stop tool_use),
                    # on démarre la task outil — elle tourne pendant que la voice WS fait du TTS.
                    if tool_task is None and tool_capture is not None and tool_capture.calls:
                        tool_task = asyncio.create_task(
                            agent.execute_captured_tools(tool_capture),
                            name="cf-tools",
                        )

                # Fallback : LLM sans préambule texte
                if tool_task is None and tool_capture is not None and tool_capture.calls:
                    tool_task = asyncio.create_task(
                        agent.execute_captured_tools(tool_capture),
                        name="cf-tools",
                    )

                # Le modèle a écrit l'appel EN TEXTE au lieu de l'émettre nativement
                # — format que le prompt statique définit lui-même par l'exemple
                # (`execute_cli(command="...")`). Constaté en usage réel : qwen3:14b
                # ne renvoie AUCUN tool_calls natif, même en non-streaming avec les
                # schémas dans le payload. On analyse donc le texte et on peuple la
                # même ToolCapture : le reste du flux (exécution parallèle puis
                # synthèse) est strictement identique au chemin natif.
                #
                # Un repli par tool_loop() a été essayé ici et retiré : il repose sur
                # le même function calling natif qui ne répond pas, et se contentait
                # donc de répéter la même phrase — l'utilisateur voyait sa demande
                # énoncée deux fois sans que rien ne s'exécute.
                if tool_task is None and tool_capture is not None and not tool_capture.calls:
                    text_calls = agent.extract_text_tool_calls(ack_text)
                    if text_calls:
                        logger.warning(
                            "Appel d'outil écrit en texte — exécution via l'analyseur",
                            route=route.value,
                            names=[n for _, n, _ in text_calls],
                        )
                        tool_capture.calls.extend(text_calls)
                        tool_task = asyncio.create_task(
                            agent.execute_captured_tools(tool_capture),
                            name="cf-tools-text",
                        )

                # Toujours rien alors que le modèle a lui-même routé [CF] —
                # c'est-à-dire qu'il annonce une ACTION. Observé en usage réel :
                # « pause ma musique » -> « C'est fait. », aucun appel écrit,
                # aucun appel natif, musique inchangée. On redemande une fois,
                # avec un prompt réduit au menu d'outils (le prompt complet fait
                # 22 Ko et noie la consigne).
                # Déclencheur : la route CF (le modèle annonce une action) OU
                # une phrase qui AFFIRME que l'action a eu lieu. Le tag seul ne
                # suffisait pas : « montre moi le cockpit » part en [I], le
                # repli ne se déclenchait pas, et « C'est lancé, le cockpit est
                # affiché. » sortait sans qu'aucun outil n'ait tourné (zéro
                # `Tool executed` dans api.log sur ce tour).
                asserts_action = claims_completion(ack_text)
                # Une réponse dégénérée (« [Cockpit] », ou vide) n'affirme rien
                # et ne porte aucun tag valide : elle échappait aux deux
                # déclencheurs ci-dessous et sortait brute à l'utilisateur.
                degenerate = is_degenerate_reply(agent.strip_text_tool_calls(ack_text))
                if (
                    tool_task is None
                    and tool_capture is not None
                    and (route is RouteEnum.CONFIRM_FIRE or asserts_action or degenerate)
                ):
                    forced = await agent.force_tool_call(message)
                    if forced:
                        logger.warning(
                            "Aucun appel dans le premier jet — relance en prompt minimal",
                            names=[n for _, n, _ in forced],
                        )
                        tool_capture.calls.extend(forced)
                        tool_task = asyncio.create_task(
                            agent.execute_captured_tools(tool_capture),
                            name="cf-tools-forced",
                        )
                    else:
                        logger.warning(
                            "Aucun outil déclenché — réponse non vérifiée",
                            route=route.value,
                            ack=ack_text[:120],
                        )

                # Aucun outil : le premier jet EST la réponse. On le rend tel quel,
                # débarrassé d'une éventuelle notation d'appel restée sans suite.
                if tool_task is None:
                    if not held:
                        return
                    text = agent.strip_text_tool_calls("".join(held))
                    # Dernier garde-fou : aucun outil n'a tourné, et la phrase
                    # affirme pourtant que l'action est faite. Laisser passer,
                    # c'est mentir à l'utilisateur — le symptôme qui revient
                    # depuis le début (musique non pausée, vue non affichée).
                    if asserts_action:
                        logger.warning(
                            "Affirmation d'action sans exécution — réponse remplacée",
                            ack=text[:120],
                        )
                        yield (
                            "Je n'ai pas réussi à déclencher l'action — aucun outil "
                            "n'a été exécuté, donc rien n'a changé. Reformule ta "
                            "demande et je réessaie."
                        )
                        return
                    # « [Cockpit] » tout seul : ce n'est pas une réponse. La
                    # rendre telle quelle donne à l'utilisateur un jeton brut
                    # sans rien lui dire de ce qui a échoué.
                    if is_degenerate_reply(text):
                        logger.warning(
                            f"Réponse dégénérée remplacée — brut : {text.strip()[:80]!r}"
                        )
                        yield (
                            "Je n'ai rien produit d'exploitable sur ce tour et aucun "
                            "outil n'a tourné. Reformule ta demande et je réessaie."
                        )
                        return
                    yield text
                    return

                # Second appel LLM pour synthétiser les résultats — avant "done"
                try:
                    results = await tool_task
                    logger.debug(f"CF tools done: {[n for _, n, _ in tool_capture.calls]}")
                    if emitted and ack_text.strip():
                        yield " "
                    # Échec total : pas de synthèse libre. Le modèle écrivait
                    # « C'est lancé » par-dessus une erreur (log du 21/09 07:19).
                    failed = all_tools_failed_message(
                        [n for _, n, _ in tool_capture.calls], list(results)
                    )
                    if failed is not None:
                        logger.warning(f"Tous les outils ont échoué — {failed}")
                        yield failed
                        return
                    synth_stream = agent.synthesize(session, ack_text, tool_capture, results)
                    _, clean_synth = await SpeedRouter.extract_route(synth_stream)
                    async for chunk in clean_synth:
                        yield chunk
                except Exception as e:
                    collector.error("JRV-GWY-001", "JRV-GWY-001", cause=e)
                    logger.opt(exception=True).error(
                        "CF tool or synthesize error",
                        error=type(e).__name__,
                        detail=str(e),
                    )
                    notifications.add(f"Outil échoué : {e}")
                    yield friendly_llm_error(e)

            return await self._finalize(session, route, _pipe(), stream)

        except Exception as e:
            collector.error("JRV-GWY-001", "JRV-GWY-001", cause=e)
            logger.opt(exception=True).error(
                "Gateway error", error=type(e).__name__, detail=str(e), session_id=str(session.id)
            )
            return session, RouteEnum.INSTANT, _fallback(e)

    async def _finalize(
        self,
        session: Session,
        route: RouteEnum,
        response: str | AsyncIterator[str],
        stream: bool,
    ) -> tuple[Session, RouteEnum, str | AsyncIterator[str]]:
        """Si stream=False : draine la réponse, ajoute l'assistant en session."""
        if stream:
            return session, route, response
        if isinstance(response, str):
            text = response
        else:
            text = "".join([chunk async for chunk in response])
        session.add_message("assistant", text)
        return session, route, text

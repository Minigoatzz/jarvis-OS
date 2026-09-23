/* Copyright (C) 2026 Barthélemy Houot
 * This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
 * See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 * home_overlays.js — ce que home.html n'avait pas :
 *
 * 1. La fenêtre d'approbation. Le serveur diffuse `approval_request` et attend
 *    une réponse (120 s pour une permission d'outil, 600 s pour une étape de
 *    mission). home.html — la page servie sur « / » — ne l'écoutait pas : rien
 *    ne s'affichait, la permission était refusée d'office à l'expiration et
 *    l'étape de mission abandonnée. Seul l'ancien index.html savait répondre.
 *
 * 2. Le retour à l'accueil depuis une vue (globe, météo…) : bouton + Échap.
 *    Jusqu'ici, seule la voix (« retour ») ramenait à l'accueil ; à l'écran il
 *    fallait recharger la page.
 *
 * Les deux vivent ici plutôt que dans home.js pour être testables seuls.
 */
(function () {
  "use strict";

  const J = window.Jarvis;
  if (!J || !J.views) return;

  // ── Retour à l'accueil ────────────────────────────────────────────────────

  function goHome() {
    const active = J.views._active;
    if (active) J.views.deactivate(active);
  }

  function isTypingSomething(target) {
    if (!target) return false;
    const editable =
      target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
    if (!editable) return false;
    const text = target.isContentEditable ? target.textContent : target.value;
    return Boolean(text && text.trim());
  }

  // Appelé par _shared.js UNIQUEMENT quand Échap n'a rien fermé d'autre
  // (palette ⌘K, Mission Control) : une pression = une seule chose fermée.
  J.onEscape = function (event) {
    if (document.body.classList.contains("approval-open")) return; // choix explicite exigé
    if (isTypingSomething(event && event.target)) return; // message en cours de saisie
    goHome();
  };

  function buildBackButton() {
    const button = document.createElement("button");
    button.type = "button";
    button.id = "home-back";
    button.className = "home-back";
    button.setAttribute("aria-label", "Retour à l'accueil");
    const arrow = document.createElement("span");
    arrow.className = "hb-arrow";
    arrow.textContent = "←";
    const label = document.createElement("span");
    label.textContent = "Accueil";
    const kbd = document.createElement("span");
    kbd.className = "hb-kbd";
    kbd.textContent = "Échap";
    button.append(arrow, label, kbd);
    button.addEventListener("click", goHome);
    document.body.appendChild(button);
  }

  // ── Approbations ──────────────────────────────────────────────────────────

  const CATEGORY_LABELS = {
    file_write: "écrire un fichier",
    file_delete: "supprimer un fichier",
    email_send: "envoyer un e-mail",
    web_agent: "piloter un navigateur",
    system_shutdown: "éteindre l'ordinateur",
    system_restart: "redémarrer l'ordinateur",
  };

  const queue = [];
  let current = null;
  let ticker = null;
  let ui = null;

  function buildModal() {
    const backdrop = document.createElement("div");
    backdrop.className = "j-approval-backdrop";
    const box = document.createElement("div");
    box.className = "j-approval";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    box.tabIndex = -1;

    const kicker = document.createElement("div");
    kicker.className = "ja-kicker";
    const dot = document.createElement("span");
    dot.className = "ja-dot";
    const kickerText = document.createElement("span");
    const position = document.createElement("span");
    position.className = "ja-position";
    kicker.append(dot, kickerText, position);

    const title = document.createElement("div");
    title.className = "ja-title";
    title.id = "ja-title";
    box.setAttribute("aria-labelledby", "ja-title");
    const desc = document.createElement("div");
    desc.className = "ja-desc";
    const status = document.createElement("div");
    status.className = "ja-status";
    status.setAttribute("aria-live", "polite");

    const btns = document.createElement("div");
    btns.className = "ja-btns";
    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "ja-btn reject";
    reject.textContent = "Refuser";
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "ja-btn approve";
    approve.textContent = "Approuver";
    btns.append(reject, approve);

    box.append(kicker, title, desc, status, btns);
    backdrop.appendChild(box);
    document.body.appendChild(backdrop);

    reject.addEventListener("click", () => answer(false));
    approve.addEventListener("click", () => answer(true));
    return { backdrop, box, kickerText, position, title, desc, status, reject, approve };
  }

  function secondsLeft(item) {
    return Math.max(0, Math.ceil((item.expiresAt - Date.now()) / 1000));
  }

  function formatLeft(s) {
    const m = Math.floor(s / 60);
    const r = String(s % 60).padStart(2, "0");
    return `${m}:${r}`;
  }

  function setButtons(enabled) {
    ui.approve.disabled = !enabled;
    ui.reject.disabled = !enabled;
  }

  function setStatus(text, bad) {
    ui.status.textContent = text;
    ui.status.classList.toggle("is-bad", Boolean(bad));
  }

  function render() {
    const msg = current.msg;
    const isPermission = Boolean(msg.action_id);
    ui.kickerText.textContent = isPermission ? "Permission requise" : "Mission · approbation d'étape";
    ui.position.textContent = queue.length ? `1 / ${queue.length + 1}` : "";
    // textContent partout : la description peut venir du modèle — jamais de HTML.
    ui.title.textContent = isPermission
      ? `Jarvis veut ${CATEGORY_LABELS[msg.category] || msg.category || "agir"}`
      : msg.project_title || msg.project_id || "Mission";
    ui.desc.textContent = msg.description || msg.step_id || "";
    setButtons(true);
    updateCountdown();
  }

  function updateCountdown() {
    if (!current || current.done) return;
    const left = secondsLeft(current);
    if (left > 0) {
      setStatus(`Sans réponse, expire dans ${formatLeft(left)}.`, false);
      return;
    }
    current.done = true;
    setButtons(false);
    setStatus(
      current.msg.action_id
        ? "Expirée : refusée automatiquement, rien n'a été fait."
        : "Expirée : cette étape de la mission n'a pas été exécutée.",
      true
    );
    setTimeout(next, 2500);
  }

  async function answer(approved) {
    if (!current || current.done) return;
    const msg = current.msg;
    setButtons(false);
    setStatus("Envoi…", false);
    try {
      let res;
      if (msg.action_id) {
        res = await J.api.post(`/api/approvals/${encodeURIComponent(msg.action_id)}/resolve`, {
          approved,
        });
      } else {
        res = await J.api.post(`/api/projects/${encodeURIComponent(msg.project_id)}/approve`, {
          step_id: msg.step_id,
          approved,
        });
      }
      current.done = true;
      if (!msg.action_id && res && res.resolved === false) {
        // Le serveur ne l'attendait plus (expirée ou mission arrêtée).
        setStatus("Trop tard : le serveur n'attendait plus cette réponse.", true);
        setTimeout(next, 2500);
        return;
      }
      setStatus(approved ? "Approuvé." : "Refusé.", false);
      setTimeout(next, 700);
    } catch (err) {
      setStatus(`Envoi impossible (${err && err.message ? err.message : err}). Réessaie.`, true);
      setButtons(true);
    }
  }

  function next() {
    current = queue.shift() || null;
    if (!current) {
      clearInterval(ticker);
      ticker = null;
      document.body.classList.remove("approval-open");
      return;
    }
    render();
  }

  function handleApprovalRequest(msg) {
    if (!msg || (!msg.action_id && !(msg.project_id && msg.step_id))) return;
    if (!ui) ui = buildModal();
    const fallback = msg.action_id ? 120 : 600;
    const ttl = Number(msg.timeout_s) > 0 ? Number(msg.timeout_s) : fallback;
    queue.push({ msg, expiresAt: Date.now() + ttl * 1000, done: false });
    if (current) {
      ui.position.textContent = `1 / ${queue.length + 1}`;
      return;
    }
    current = queue.shift();
    document.body.classList.add("approval-open");
    render();
    ui.box.focus();
    if (!ticker) ticker = setInterval(updateCountdown, 1000);
  }

  function init() {
    buildBackButton();
  }
  if (document.body) init();
  else document.addEventListener("DOMContentLoaded", init);

  window.JarvisOverlays = { handleApprovalRequest, goHome };
})();

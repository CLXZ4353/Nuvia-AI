import { formatDate, formatNewsTitle, formatSourceLabel, formatWeekRangeLabel, safeExternalUrl, setText } from "./dom.js";

function cloneTemplate(id) {
  const template = document.getElementById(id);
  const root = template?.content?.firstElementChild?.cloneNode(true);
  if (!root) throw new Error(`Template UI non disponibile: ${id}`);
  return root;
}

function setCardSaveState(card, item) {
  const button = card.querySelector('[data-action="toggle-save"]');
  const saved = Boolean(item.saved);
  card.classList.toggle("is-saved", saved);
  if (!button) return;
  button.setAttribute("aria-pressed", String(saved));
  button.setAttribute("aria-label", saved ? "Rimuovi dagli elementi salvati" : "Salva negli elementi salvati");
  button.title = saved ? "Rimuovi dagli elementi salvati" : "Salva negli elementi salvati";
}

export function createDigestCard(item, { onToggleSave, onOpenAssistant } = {}) {
  const card = cloneTemplate("digest-card-template");
  card.dataset.cardId = String(item.id || "");
  setText(card, '[data-field="title"]', formatNewsTitle(item.title));
  setText(card, '[data-field="category"]', item.category || "Non classificata");
  setText(card, '[data-field="source"]', item.source || "Fonte non disponibile");
  setText(card, '[data-field="summary"]', item.summary || "Sintesi non disponibile.");
  setText(card, '[data-field="why"]', item.why || "Dettaglio non disponibile.");

  const sourceLink = card.querySelector('[data-field="link"]');
  const sourceUrl = safeExternalUrl(item.url);
  setText(card, '[data-field="linkLabel"]', formatSourceLabel(item.source, sourceUrl));
  if (sourceLink && sourceUrl) {
    sourceLink.href = sourceUrl;
  } else if (sourceLink) {
    sourceLink.removeAttribute("href");
    sourceLink.setAttribute("aria-disabled", "true");
    sourceLink.classList.add("is-disabled");
  }

  setCardSaveState(card, item);
  card.querySelector('[data-action="toggle-save"]')?.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    onToggleSave?.(item);
  });
  card.querySelector('[data-action="open-assistant"]')?.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    onOpenAssistant?.(item);
  });
  return card;
}

export function renderDigest(container, items, { onToggleSave, onOpenAssistant, focus = false } = {}) {
  const fragment = document.createDocumentFragment();
  for (const item of Array.isArray(items) ? items : []) {
    const card = createDigestCard(item, { onToggleSave, onOpenAssistant });
    if (focus) card.classList.add("is-focused-card");
    fragment.append(card);
  }
  container.replaceChildren(fragment);
  container.setAttribute("aria-busy", "false");
  container.classList.toggle("focused-digest-view", Boolean(focus));
  container.classList.toggle("history-digest-view", Boolean(focus));
}

export function renderDigestState(root, { kind = "loading", message = "", retry = false } = {}) {
  if (!root) return;
  root.dataset.state = kind;
  root.className = `content-state content-state--${kind}`;
  const messageNode = root.querySelector('[data-ui="digest-message"]');
  if (messageNode) messageNode.textContent = message;
  const retryButton = root.querySelector('[data-action="retry-digest"]');
  if (retryButton) retryButton.hidden = !retry;
  root.hidden = kind === "ready";
}

export function renderSaved(container, stateNode, items, { onOpen } = {}) {
  const fragment = document.createDocumentFragment();
  const records = Array.isArray(items) ? items : [];
  records.forEach((item) => {
    const button = cloneTemplate("saved-news-template");
    button.dataset.newsId = String(item.id || "");
    setText(button, '[data-field="title"]', formatNewsTitle(item.title));
    const date = String(item.publication_date || item.digest_date || "");
    const dateNode = setText(button, '[data-field="date"]', date ? formatDate(date) : "");
    if (dateNode) {
      dateNode.hidden = !date;
      if (date) dateNode.setAttribute("datetime", date);
    }
    button.addEventListener("click", () => onOpen?.(item));
    fragment.append(button);
  });
  container.replaceChildren(fragment);
  container.setAttribute("aria-busy", "false");
  if (stateNode) {
    stateNode.textContent = records.length ? "" : "Nessuna notizia salvata.";
    stateNode.hidden = records.length > 0;
  }
}

export function renderHistory(container, stateNode, items, { digestHref } = {}) {
  const fragment = document.createDocumentFragment();
  const records = Array.isArray(items) ? items : [];
  records.forEach((digest, index) => {
    const row = cloneTemplate("history-digest-template");
    row.dataset.digestId = String(digest.id || "");
    const label = formatWeekRangeLabel(digest.week_start, digest.week_end, { prefix: "Week: ", includeYear: true })
      || (digest.digest_date ? formatDate(digest.digest_date) : (digest.title || "Digest"));
    setText(row, '[data-field="date"]', label);
    const list = row.querySelector('[data-field="items"]');
    const previews = Array.isArray(digest.previews) ? digest.previews : [];
    const previewFragment = document.createDocumentFragment();
    previews.forEach((item) => {
      const preview = cloneTemplate("history-news-item-template");
      setText(preview, '[data-field="title"]', formatNewsTitle(item.title || item.source, "Notizia"));
      previewFragment.append(preview);
    });
    list?.replaceChildren(previewFragment);
    const toggle = row.querySelector('[data-action="toggle-history"]');
    const panel = row.querySelector('[data-field="panel"]');
    const panelId = `history-panel-${index}`;
    const toggleId = `history-toggle-${index}`;
    if (toggle && panel) {
      toggle.id = toggleId;
      toggle.setAttribute("aria-controls", panelId);
      panel.id = panelId;
      panel.setAttribute("aria-labelledby", toggleId);
    }

    const panelFocusable = panel
      ? [...panel.querySelectorAll('a[href], button:not([disabled])')]
      : [];
    const setExpanded = (expanded) => {
      toggle?.setAttribute("aria-expanded", String(expanded));
      row.classList.toggle("open", expanded);
      if (panel) {
        panel.inert = !expanded;
        panel.setAttribute("aria-hidden", String(!expanded));
      }
      panelFocusable.forEach((node) => { node.tabIndex = expanded ? 0 : -1; });
    };
    setExpanded(false);
    toggle?.addEventListener("click", () => {
      setExpanded(toggle.getAttribute("aria-expanded") !== "true");
    });
    const readMore = row.querySelector('[data-action="open-digest"]');
    const href = digestHref?.(digest);
    if (readMore && href) readMore.href = href;
    fragment.append(row);
  });
  container.replaceChildren(fragment);
  container.setAttribute("aria-busy", "false");
  if (stateNode) {
    stateNode.textContent = records.length ? "" : "Nessun digest trovato.";
    stateNode.hidden = records.length > 0;
  }
}

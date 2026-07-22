import { ApiClient, ApiError } from "./api.js";
import { loadRuntimeConfig } from "./config.js";
import {
  element,
  formatDate,
  formatWeekRangeLabel,
  safeExternalUrl,
  setButtonBusy,
  showLiveMessage,
} from "./dom.js";
import { renderDigest, renderDigestState, renderHistory, renderSaved } from "./render.js";
import { createStore } from "./state.js";
import { createVisualSystem } from "./animation.js";

const refs = {
  sidebar: document.getElementById("sidebar"),
  sidebarToggle: document.getElementById("sidebarToggle"),
  canvasContainer: document.getElementById("canvas-container"),
  canvas: document.getElementById("network"),
  robot: document.getElementById("robot-container"),
  assistantPage: document.querySelector('[data-ui="assistant-page"]'),
  assistantComposer: document.querySelector('[data-ui="assistant-composer"]'),
  assistantContext: document.querySelector('[data-ui="assistant-context"]'),
  assistantContextTitle: document.querySelector('[data-ui="assistant-context-title"]'),
  assistantContextLink: document.querySelector('[data-ui="assistant-context-link"]'),
  assistantContextClose: document.querySelector('[data-ui="assistant-context-close"]'),
  assistantMessageList: document.querySelector('[data-ui="assistant-message-list"]'),
  assistantInput: document.querySelector('[data-ui="assistant-input"]'),
  assistantSend: document.querySelector('[data-ui="assistant-send"]'),
  assistantStatus: document.querySelector('[data-ui="assistant-status"]'),
  assistantAttachmentButton: document.querySelector('[data-ui="assistant-attachment-button"]'),
  assistantAttachmentInput: document.querySelector('[data-ui="assistant-attachment-input"]'),
  assistantAttachmentPreview: document.querySelector('[data-ui="assistant-attachment-preview"]'),
  assistantAttachmentName: document.querySelector('[data-ui="assistant-attachment-name"]'),
  assistantAttachmentRemove: document.querySelector('[data-ui="assistant-attachment-remove"]'),
  assistantDropZone: document.querySelector('[data-ui="assistant-drop-zone"]'),
  assistantDropOverlay: document.querySelector('[data-ui="assistant-drop-overlay"]'),
  assistantRecordButton: document.querySelector('[data-ui="assistant-record-button"]'),
  assistantRecordingBar: document.querySelector('[data-ui="assistant-recording-bar"]'),
  assistantRecordingTime: document.querySelector('[data-ui="assistant-recording-time"]'),
  assistantRecordingCancel: document.querySelector('[data-ui="assistant-recording-cancel"]'),
  assistantRecordingStop: document.querySelector('[data-ui="assistant-recording-stop"]'),
  digestPeriod: document.querySelector('[data-ui="digest-period"]'),
  digestList: document.querySelector('[data-ui="digest-list"]'),
  digestState: document.querySelector('[data-ui="digest-state"]'),
  savedList: document.querySelector('[data-ui="saved-list"]'),
  savedState: document.querySelector('[data-ui="saved-state"]'),
  historyList: document.querySelector('[data-ui="history-list"]'),
  historyState: document.querySelector('[data-ui="history-state"]'),
  historySearch: document.querySelector('[data-ui="history-search"]'),
  appFeedback: document.querySelector('[data-ui="app-feedback"]'),
  settingsPage: document.getElementById("settings-page"),
  settingsForm: document.getElementById("settings-form"),
  settingsTitle: document.getElementById("settings-page-title"),
  settingsFeedback: document.querySelector('[data-ui="settings-feedback"]'),
  authPage: document.getElementById("auth-page"),
  authLogoLink: document.querySelector("[data-auth-logo-link]"),
  authForm: document.getElementById("auth-form"),
  authFeedback: document.querySelector('[data-ui="auth-feedback"]'),
  authPanel: document.querySelector(".auth-panel"),
};

const store = createStore({
  runtime: null,
  user: null,
  digest: null,
  visible: { kind: "current", items: [], digestDate: "" },
  saved: [],
  history: [],
  historyNextOffset: null,
  historyQuery: "",
  settings: null,
  authMode: "login",
});

let api = null;
let visualSystem = null;
let visualRefreshFrame = 0;
let visualPostPaintFrame = 0;
let historyTimer = 0;
let historyRequestVersion = 0;
let historyAbortController = null;
let feedbackTimer = 0;
let modalReturnFocus = null;
let activeSidebarSection = "home";
let sidebarSectionBeforeSettings = "home";
let pushRuntime = null;
let assistantActiveContext = null;
let assistantPreviousContext = null;
let assistantHistory = [];
let assistantPending = false;
let assistantRequestVersion = 0;
let assistantAttachment = null;
let assistantMediaRecorder = null;
let assistantRecordingChunks = [];
let assistantRecordingStream = null;
let assistantRecordingMimeType = "";
let assistantRecordingStartedAt = 0;
let assistantRecordingTimerHandle = 0;
let assistantRecordingCancelled = false;
let assistantRecordingStopResolvers = null;

const ASSISTANT_AVATAR_SRC = "/assets/images/nuvia-assistant-avatar.png?v=20260722-avatar-box-restored";
const saveRequests = new Set();
const BRAND_FONT_DESCRIPTOR = '400 180px "Alex Brush"';
const BRAND_FONT_TIMEOUT_MS = 2_500;
const ASSISTANT_CONTEXT_STORAGE_KEY = "nuvia.assistant.active-news.v1";
const ASSISTANT_CONTEXT_PROMPT = "Che cosa vorresti approfondire di questa notizia?";
const ASSISTANT_COMPARISON_PATTERN = /\b(?:confronta|confrontare|confronto|compara|comparare|paragona|paragonare|paragone|differenze?|rispetto\s+(?:a|al|alla|alle|agli)|entrambe|due\s+notizie)\b/i;
const ASSISTANT_ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024;
const ASSISTANT_ATTACHMENT_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "application/pdf",
  "text/plain",
]);
const ASSISTANT_ATTACHMENT_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".pdf", ".txt"]);
const ASSISTANT_DEFAULT_ATTACHMENT_MESSAGE = "Analizza il file allegato.";
const ASSISTANT_ATTACHMENT_CLARIFY_QUESTION = "Quali aspetti vorresti approfondire?";

// Voice messages: separate, dedicated limits/format allow-list (kept apart from the
// generic file-attachment ones above), matching the server-side ASSISTANT_AUDIO_* limits.
const ASSISTANT_AUDIO_MAX_BYTES = 15 * 1024 * 1024;
const ASSISTANT_AUDIO_MAX_DURATION_MS = 3 * 60 * 1000;
const ASSISTANT_RECORDING_MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/mp4",
];

function query(selector, root = document) {
  return root.querySelector(selector);
}

async function waitForBrandFont() {
  if (!document.fonts?.load) return;
  try {
    await Promise.race([
      document.fonts.load(BRAND_FONT_DESCRIPTOR, "Nuvia.ai"),
      new Promise((resolve) => window.setTimeout(resolve, BRAND_FONT_TIMEOUT_MS)),
    ]);
  } catch {
    // A failed font request must not leave the interface permanently hidden.
  }
}

function waitForNextPaint() {
  return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}

function todayIso() {
  const timeZone = String(store.get().runtime?.timezone || "UTC");
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date());
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    if (values.year && values.month && values.day) {
      return `${values.year}-${values.month}-${values.day}`;
    }
  } catch {
    // Use UTC only when the configured timezone is not recognised by the browser.
  }
  return new Date().toISOString().slice(0, 10);
}

function digestSelectionFromLocation() {
  const params = new URLSearchParams(window.location.search);
  const digestDate = String(params.get("date") || "").trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(digestDate)) return { date: digestDate };
  const digestId = String(params.get("id") || "").trim();
  if (digestId && digestId.length <= 512 && !/[\u0000\r\n]/.test(digestId)) return { id: digestId };
  return null;
}

function digestLocationFor(summary) {
  const digestDate = String(summary?.digest_date || "").trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(digestDate)) {
    return `/digest?${new URLSearchParams({ date: digestDate }).toString()}`;
  }
  const digestId = String(summary?.id || "").trim();
  return digestId ? `/digest?${new URLSearchParams({ id: digestId }).toString()}` : "/digest";
}

function ensureVisualSystem() {
  if (visualSystem) {
    return visualSystem;
  }

  visualSystem = createVisualSystem({
    canvas: refs.canvas,
    container: refs.canvasContainer,
    overlay: refs.digestList,
    robot: refs.robot,
  });
  return visualSystem;
}

function cancelScheduledVisualRefresh() {
  if (visualRefreshFrame) {
    window.cancelAnimationFrame(visualRefreshFrame);
    visualRefreshFrame = 0;
  }
  if (visualPostPaintFrame) {
    window.cancelAnimationFrame(visualPostPaintFrame);
    visualPostPaintFrame = 0;
  }
}

function refreshVisualSystem() {
  if (!visualSystem) {
    if (document.body.dataset.appState !== "ready") {
      return;
    }
    ensureVisualSystem();
    return;
  }
  visualSystem.refresh();
}

function scheduleVisualRefresh({ afterPaint = false } = {}) {
  if (visualRefreshFrame || visualPostPaintFrame) {
    return;
  }

  visualRefreshFrame = window.requestAnimationFrame(() => {
    visualRefreshFrame = 0;
    if (!afterPaint) {
      refreshVisualSystem();
      return;
    }

    visualPostPaintFrame = window.requestAnimationFrame(() => {
      visualPostPaintFrame = 0;
      refreshVisualSystem();
    });
  });
}

function showAppFeedback(message, { error = false, timeout = 4_500 } = {}) {
  const node = refs.appFeedback;
  if (!node) return;
  window.clearTimeout(feedbackTimer);
  node.hidden = !message;
  node.textContent = message || "";
  node.classList.toggle("is-error", Boolean(error));
  if (message && timeout) {
    feedbackTimer = window.setTimeout(() => {
      node.hidden = true;
      node.textContent = "";
    }, timeout);
  }
}

function normalizeItems(items) {
  return Array.isArray(items) ? items.filter((item) => item && typeof item === "object") : [];
}

function replaceSaveState(items, newsId, saved) {
  return normalizeItems(items).map((item) => (
    item.id === newsId ? { ...item, saved } : item
  ));
}

function renderDigestPeriod(weekStart, weekEnd) {
  if (!refs.digestPeriod) return;
  const label = formatWeekRangeLabel(weekStart, weekEnd);
  refs.digestPeriod.textContent = label;
  refs.digestPeriod.hidden = !label;
}

function renderVisibleDigest() {
  const state = store.get();
  const visible = state.visible || { kind: "current", items: [] };
  const items = normalizeItems(visible.items);
  renderDigestPeriod(visible.weekStart, visible.weekEnd);
  if (!items.length) {
    refs.digestList?.replaceChildren();
    refs.digestList?.setAttribute("aria-busy", "false");
    renderDigestState(refs.digestState, {
      kind: "empty",
      message: visible.kind === "current"
        ? "Nessuna notizia disponibile nel digest corrente."
        : "Nessuna notizia disponibile per questa selezione.",
      retry: visible.kind === "current",
    });
    scheduleVisualRefresh({ afterPaint: true });
    return;
  }
  renderDigest(refs.digestList, items, {
    focus: visible.kind === "saved",
    onToggleSave: toggleSavedNews,
    onOpenAssistant: openAssistantForNews,
  });
  renderDigestState(refs.digestState, { kind: "ready" });
  scheduleVisualRefresh({ afterPaint: true });
}

function hideDigestState() {
  if (refs.digestState) refs.digestState.hidden = true;
}

function renderSavedNews() {
  renderSaved(refs.savedList, refs.savedState, store.get().saved, { onOpen: openSavedNews });
}

function renderHistoryList() {
  const state = store.get();
  renderHistory(refs.historyList, refs.historyState, state.history, { digestHref: digestLocationFor });
  if (state.historyNextOffset !== null && refs.historyList) {
    const button = element("button", {
      className: "sidebar-load-more",
      text: "Carica altri digest",
      attributes: { type: "button" },
    });
    button.addEventListener("click", () => void loadHistory({ reset: false }));
    refs.historyList.append(button);
  }
}

function setSidebarActive(section) {
  activeSidebarSection = section;
  document.querySelectorAll("[data-sidebar-key]").forEach((node) => {
    const active = node.getAttribute("data-sidebar-key") === section;
    node.classList.toggle("is-active", active);
    if (node.matches("a")) {
      if (active) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    } else {
      node.setAttribute("aria-pressed", String(active));
    }
  });
}

function setSidebarSection(section, { updateActive = true } = {}) {
  document.querySelectorAll("[data-sidebar-panel]").forEach((panel) => {
    panel.classList.toggle("sidebar-panel-hidden", panel.getAttribute("data-sidebar-panel") !== section);
  });
  if (updateActive) setSidebarActive(section);
}

function setAssistantView(active) {
  const visible = Boolean(active);
  if (!visible && assistantMediaRecorder) void stopAssistantVoiceRecording({ cancelled: true });
  document.body.classList.toggle("assistant-view", visible);
  if (refs.assistantPage) {
    refs.assistantPage.hidden = !visible;
    refs.assistantPage.setAttribute("aria-hidden", String(!visible));
  }
  refs.canvasContainer?.setAttribute(
    "aria-label",
    visible ? "Nuvia Assistant" : "Digest di ricerca",
  );
  if (document.body.dataset.appState === "ready") {
    scheduleVisualRefresh({ afterPaint: true });
  }
}

function setSidebarOpen(open) {
  if (!refs.sidebar || !refs.sidebarToggle) return;
  refs.sidebar.classList.toggle("closed", !open);
  refs.sidebarToggle.setAttribute("aria-expanded", String(open));
  scheduleVisualRefresh();
}

function initializeSidebar() {
  const assistantActive = location.pathname === "/assistant";
  const assistantDesktop = assistantActive && !window.matchMedia(
    "(max-width: 1200px), (max-height: 760px)",
  ).matches;
  if (assistantDesktop) refs.sidebar?.classList.remove("closed");
  refs.sidebar?.classList.remove("initializing");
  refs.sidebar?.classList.add("transitions-enabled");
  setAssistantView(assistantActive);
  const initialPanel = location.pathname === "/saved" ? "saved" : "history";
  setSidebarSection(initialPanel, { updateActive: false });
  setSidebarActive(assistantActive ? "assistant" : location.pathname === "/saved" ? "saved" : "home");
  refs.sidebarToggle?.addEventListener("click", () => {
    setSidebarOpen(refs.sidebar?.classList.contains("closed"));
  });
  document.querySelectorAll("[data-sidebar-section]").forEach((button) => {
    button.addEventListener("click", () => {
      const section = button.getAttribute("data-sidebar-section") || "history";
      setSidebarSection(section);
      if (refs.sidebar?.classList.contains("closed")) setSidebarOpen(true);
    });
  });
  refs.historySearch?.addEventListener("input", () => {
    window.clearTimeout(historyTimer);
    historyTimer = window.setTimeout(() => void loadHistory({ reset: true }), 250);
  });
}

function normalizeAssistantContext(item) {
  if (!item || typeof item !== "object") return null;
  const title = String(item.title || "").trim().slice(0, 500);
  const summary = String(item.summary || "").trim();
  const why = String(item.why || "").trim();
  const organizedContent = [
    summary ? `Sintesi completa disponibile:\n${summary}` : "",
    why ? `Perché conta:\n${why}` : "",
  ].filter(Boolean).join("\n\n");
  const content = String(item.content || organizedContent).trim().slice(0, 16_000);
  const source = String(item.source || "").trim().slice(0, 300);
  const rawUrl = String(item.url || "").trim();
  const url = rawUrl ? safeExternalUrl(rawUrl) : "";
  if (!title && !content) return null;
  return {
    id: String(item.id || "").trim().slice(0, 512),
    title,
    content,
    source,
    url,
  };
}

function storedAssistantContext() {
  try {
    const rawValue = window.sessionStorage.getItem(ASSISTANT_CONTEXT_STORAGE_KEY);
    return rawValue ? normalizeAssistantContext(JSON.parse(rawValue)) : null;
  } catch {
    return null;
  }
}

function persistAssistantContext(context) {
  try {
    if (context) window.sessionStorage.setItem(ASSISTANT_CONTEXT_STORAGE_KEY, JSON.stringify(context));
    else window.sessionStorage.removeItem(ASSISTANT_CONTEXT_STORAGE_KEY);
  } catch {
    // The chat remains usable when storage is unavailable or disabled.
  }
}

function appendAssistantInlineMarkdown(parent, value) {
  const source = String(value || "");
  const tokenPattern = /(\*\*[^*\n]+\*\*|__[^_\n]+__|\[[^\]\n]+\]\([^)\n]+\)|\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`)/g;
  let cursor = 0;
  for (const match of source.matchAll(tokenPattern)) {
    const index = Number(match.index || 0);
    if (index > cursor) parent.append(document.createTextNode(source.slice(cursor, index)));
    const token = match[0];
    const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (linkMatch) {
      const rawUrl = linkMatch[2].trim().replace(/^<|>$/g, "");
      const href = /^https?:\/\//i.test(rawUrl) ? safeExternalUrl(rawUrl) : "";
      if (href) {
        const link = element("a", {
          className: "assistant-markdown-link",
          attributes: { href, target: "_blank", rel: "noopener noreferrer" },
        });
        appendAssistantInlineMarkdown(link, linkMatch[1]);
        parent.append(link);
      } else {
        appendAssistantInlineMarkdown(parent, linkMatch[1]);
      }
    } else if ((token.startsWith("**") && token.endsWith("**"))
      || (token.startsWith("__") && token.endsWith("__"))) {
      const strong = element("strong");
      appendAssistantInlineMarkdown(strong, token.slice(2, -2));
      parent.append(strong);
    } else if (token.startsWith("`") && token.endsWith("`")) {
      parent.append(element("span", { className: "assistant-inline-code", text: token.slice(1, -1) }));
    } else {
      const emphasis = element("em");
      appendAssistantInlineMarkdown(emphasis, token.slice(1, -1));
      parent.append(emphasis);
    }
    cursor = index + token.length;
  }
  if (cursor < source.length) parent.append(document.createTextNode(source.slice(cursor)));
}

function renderAssistantMarkdown(container, value) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = null;
  let list = null;
  let listType = "";
  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      paragraph = null;
      list = null;
      listType = "";
      continue;
    }

    const headingMatch = line.match(/^\s*(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      paragraph = null;
      list = null;
      listType = "";
      const headingLevel = Math.min(6, headingMatch[1].length + 2);
      const heading = element(`h${headingLevel}`, { className: "assistant-markdown-heading" });
      appendAssistantInlineMarkdown(heading, headingMatch[2].trim());
      container.append(heading);
      continue;
    }

    const unorderedMatch = line.match(/^\s*[-+*]\s+(.+)$/);
    const orderedMatch = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unorderedMatch || orderedMatch) {
      paragraph = null;
      const nextListType = orderedMatch ? "ol" : "ul";
      if (!list || listType !== nextListType) {
        list = element(nextListType, { className: "assistant-markdown-list" });
        listType = nextListType;
        container.append(list);
      }
      const item = element("li");
      appendAssistantInlineMarkdown(item, (orderedMatch || unorderedMatch)[1].trim());
      list.append(item);
      continue;
    }

    list = null;
    listType = "";
    if (!paragraph) {
      paragraph = element("p");
      container.append(paragraph);
    } else {
      paragraph.append(element("br"));
    }
    appendAssistantInlineMarkdown(paragraph, line.trim());
  }
}

function scrollAssistantToLatest() {
  window.requestAnimationFrame(() => {
    refs.assistantMessageList?.scrollTo({
      top: refs.assistantMessageList.scrollHeight,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
  });
}

function buildAssistantAvatar() {
  const avatar = element("span", {
    className: "assistant-message-avatar",
    attributes: { "aria-hidden": "true" },
  });
  avatar.append(element("img", {
    className: "assistant-message-avatar-image",
    attributes: { src: ASSISTANT_AVATAR_SRC, alt: "" },
  }));
  return avatar;
}

function assistantMessage(role, content, { error = false, loading = false } = {}) {
  const classNames = ["assistant-message"];
  if (role === "user") classNames.push("assistant-message-user");
  else classNames.push("assistant-message-nuvia", "assistant-message-assistant");
  if (error) classNames.push("assistant-message-error");
  if (loading) classNames.push("assistant-message-loading");
  const bubble = element("div", {
    className: classNames.join(" "),
    attributes: { "aria-label": role === "user" ? "Messaggio utente" : "Messaggio Nuvia" },
  });
  const shell = element("div", { className: "assistant-message-shell" });
  if (role !== "user") {
    shell.append(buildAssistantAvatar());
  }
  const bubbleSurface = element("div", { className: "assistant-message-bubble" });
  const contentNode = element("div", { className: "assistant-message-content" });
  if (loading) {
    const paragraph = element("p");
    paragraph.append(element("span", { className: "assistant-loading-dots", text: content }));
    contentNode.append(paragraph);
  } else {
    renderAssistantMarkdown(contentNode, content);
  }
  bubbleSurface.append(contentNode);
  shell.append(bubbleSurface);
  bubble.append(shell);
  refs.assistantMessageList?.append(bubble);
  scrollAssistantToLatest();
  return bubble;
}

let assistantAudioSeq = 0;

/** Inline SVG markup for the small set of icons used by the audio player and the
 *  text/audio format choice, drawn in the same style (24x24 viewBox, currentColor,
 *  rounded strokes) as the rest of the app's icon set (see index.html). */
const ASSISTANT_ICON_PATHS = {
  play: '<path d="M8 5.5v13l10-6.5z" fill="currentColor" stroke="none"></path>',
  pause:
    '<rect x="6" y="5" width="4" height="14" rx="1.2" fill="currentColor" stroke="none"></rect>' +
    '<rect x="14" y="5" width="4" height="14" rx="1.2" fill="currentColor" stroke="none"></rect>',
  volume:
    '<path d="M4 9.5v5h3.6l4.9 4V5.5l-4.9 4H4z"></path>' +
    '<path d="M16.1 8.9a4.6 4.6 0 0 1 0 6.2"></path>' +
    '<path d="M18.6 6.4a8.2 8.2 0 0 1 0 11.2"></path>',
  muted:
    '<path d="M4 9.5v5h3.6l4.9 4V5.5l-4.9 4H4z"></path>' +
    '<path d="m16.3 9.7 4.4 4.4"></path>' +
    '<path d="m20.7 9.7-4.4 4.4"></path>',
  document:
    '<path d="M7 3.5h7l4 4V19a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z"></path>' +
    '<path d="M14 3.5V8h4"></path>' +
    '<path d="M9 12.5h6"></path>' +
    '<path d="M9 16h6"></path>',
  headphones:
    '<path d="M4 14v-2a8 8 0 0 1 16 0v2"></path>' +
    '<rect x="3" y="13.5" width="4" height="6" rx="1.4"></rect>' +
    '<rect x="17" y="13.5" width="4" height="6" rx="1.4"></rect>',
};

function assistantIcon(name, { className = "" } = {}) {
  const wrapper = element("span", { className: `assistant-icon-svg ${className}`.trim() });
  wrapper.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    (ASSISTANT_ICON_PATHS[name] || "") +
    "</svg>";
  return wrapper;
}

function formatAssistantAudioTime(seconds) {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const minutes = String(Math.floor(total / 60)).padStart(2, "0");
  const secs = String(total % 60).padStart(2, "0");
  return `${minutes}:${secs}`;
}

/** Builds a compact, design-system-consistent audio player: a Play/Pause button, a
 *  seek/progress bar, an elapsed/total time readout ("00:25 / 02:10"), and a small
 *  icon+slider volume control. The transcript text is kept internally (for retries/
 *  regeneration and conversation history) but intentionally never rendered in the UI -
 *  per the assistant's audio-answer requirements, only playback controls are shown. */
function buildAssistantAudioPlayer(audioBase64, mime, transcriptText, { onRegenerate } = {}) {
  const uid = `assistant-audio-${++assistantAudioSeq}`;
  const wrapper = element("div", { className: "assistant-audio-player" });
  // Kept for history reconciliation / regeneration only - never displayed.
  wrapper._transcriptText = transcriptText;

  const audio = new Audio(`data:${mime || "audio/wav"};base64,${audioBase64}`);
  audio.preload = "metadata";

  const controls = element("div", { className: "assistant-audio-controls" });

  const playButton = element("button", {
    className: "assistant-audio-btn assistant-audio-btn-play",
    attributes: { type: "button", "aria-label": "Riproduci", "aria-pressed": "false", id: `${uid}-play` },
  });
  playButton.append(assistantIcon("play"));

  const seekBar = element("input", {
    className: "assistant-audio-seek",
    attributes: {
      type: "range",
      min: "0",
      max: "0",
      step: "0.1",
      value: "0",
      "aria-label": "Posizione nella traccia audio",
      id: `${uid}-seek`,
    },
  });

  const timeLabel = element("span", { className: "assistant-audio-time", text: "00:00 / 00:00" });

  const volumeGroup = element("div", { className: "assistant-audio-volume-group" });
  const volumeButton = element("button", {
    className: "assistant-audio-btn assistant-audio-btn-volume",
    attributes: { type: "button", "aria-label": "Disattiva audio", "aria-pressed": "false" },
  });
  volumeButton.append(assistantIcon("volume"));
  volumeGroup.append(volumeButton);

  controls.append(playButton, seekBar, timeLabel, volumeGroup);

  const setPlayIcon = (playing) => {
    playButton.replaceChildren(assistantIcon(playing ? "pause" : "play"));
    playButton.setAttribute("aria-label", playing ? "Pausa" : "Riproduci");
    playButton.setAttribute("aria-pressed", String(playing));
  };

  const setVolumeIcon = (muted) => {
    volumeButton.replaceChildren(assistantIcon(muted ? "muted" : "volume"));
    volumeButton.setAttribute("aria-label", muted ? "Riattiva audio" : "Disattiva audio");
    volumeButton.setAttribute("aria-pressed", String(muted));
  };

  let cachedDuration = 0;
  const updateTimeLabel = () => {
    timeLabel.textContent = `${formatAssistantAudioTime(audio.currentTime)} / ${formatAssistantAudioTime(cachedDuration)}`;
    const progress = cachedDuration > 0 ? Math.min(100, Math.max(0, (audio.currentTime / cachedDuration) * 100)) : 0;
    seekBar.style.setProperty("--audio-progress", `${progress}%`);
  };

  const resetToStart = () => {
    seekBar.value = "0";
    setPlayIcon(false);
    updateTimeLabel();
  };

  playButton.addEventListener("click", () => {
    if (audio.paused) audio.play().catch(() => {});
    else audio.pause();
  });
  seekBar.addEventListener("input", () => {
    audio.currentTime = Number(seekBar.value) || 0;
    updateTimeLabel();
  });
  volumeButton.addEventListener("click", () => {
    audio.muted = !audio.muted;
    if (!audio.muted && audio.volume === 0) audio.volume = 1;
    setVolumeIcon(audio.muted);
  });

  audio.addEventListener("loadedmetadata", () => {
    cachedDuration = Number.isFinite(audio.duration) ? audio.duration : 0;
    seekBar.max = String(cachedDuration);
    updateTimeLabel();
  });
  audio.addEventListener("timeupdate", () => {
    if (!seekBar.matches(":active")) seekBar.value = String(audio.currentTime);
    updateTimeLabel();
  });
  audio.addEventListener("play", () => setPlayIcon(true));
  audio.addEventListener("pause", () => setPlayIcon(false));
  // Use the native "ended" event (not an estimate-based timer) to reliably reset the
  // player to the very start once playback finishes, ready to be replayed immediately.
  audio.addEventListener("ended", () => {
    audio.currentTime = 0;
    resetToStart();
  });
  audio.addEventListener("error", () => {
    setAssistantAudioError(wrapper, "La riproduzione dell'audio non è riuscita.", { onRegenerate });
  });

  wrapper.append(controls);

  wrapper._assistantAudio = audio;
  return wrapper;
}

function setAssistantAudioError(wrapper, message, { onRegenerate } = {}) {
  wrapper.querySelectorAll(".assistant-audio-error").forEach((node) => node.remove());
  const errorBox = element("div", { className: "assistant-audio-error", attributes: { role: "alert" } });
  errorBox.append(element("span", { text: message }));
  const retryButton = element("button", {
    className: "assistant-audio-retry",
    attributes: { type: "button" },
    text: "Rigenera audio",
  });
  retryButton.addEventListener("click", () => onRegenerate?.());
  errorBox.append(retryButton);
  wrapper.prepend(errorBox);
}

/** Renders the "Come preferisci ricevere la risposta?" choice and wires up text/audio delivery. */
function assistantFormatChoiceMessage(answerText, requestPayload = null) {
  const bubble = assistantMessage("assistant", "Come preferisci ricevere la risposta?", {});
  const shell = bubble.querySelector(".assistant-message-shell");
  const bubbleSurface = bubble.querySelector(".assistant-message-bubble");
  const contentNode = bubble.querySelector(".assistant-message-content");

  const messageStack = element("div", { className: "assistant-message-stack assistant-format-stack" });
  if (shell && bubbleSurface) {
    messageStack.append(bubbleSurface);
    shell.append(messageStack);
  }

  const choiceRow = element("div", {
    className: "assistant-format-choice",
    attributes: { role: "group", "aria-label": "Formato della risposta" },
  });
  const textButton = element("button", {
    className: "assistant-format-button assistant-format-button-text",
    attributes: { type: "button" },
  });
  textButton.append(assistantIcon("document", { className: "assistant-format-icon" }), element("span", { text: "Testo" }));
  const audioButton = element("button", {
    className: "assistant-format-button assistant-format-button-audio",
    attributes: { type: "button" },
  });
  audioButton.append(assistantIcon("headphones", { className: "assistant-format-icon" }), element("span", { text: "Audio" }));
  choiceRow.append(textButton, audioButton);
  (shell && bubbleSurface ? messageStack : bubble).append(choiceRow);
  scrollAssistantToLatest();

  let choiceMade = false;
  const disableChoice = () => {
    textButton.disabled = true;
    audioButton.disabled = true;
  };
  const removeChoice = () => choiceRow.remove();

  textButton.addEventListener("click", () => {
    if (choiceMade) return;
    choiceMade = true;
    textButton.classList.add("is-selected");
    disableChoice();
    removeChoice();
    contentNode.replaceChildren();
    renderAssistantMarkdown(contentNode, answerText);
    setAssistantStatus();
    scrollAssistantToLatest();
  });

  /** Replaces the last matching assistant turn in history with the (expanded) audio text,
   *  so follow-up questions stay consistent with what was actually delivered to the user. */
  const reconcileHistoryWithAudioAnswer = (audioAnswer) => {
    for (let index = assistantHistory.length - 1; index >= 0; index -= 1) {
      if (assistantHistory[index].role === "assistant" && assistantHistory[index].content === answerText) {
        assistantHistory[index] = { role: "assistant", content: audioAnswer };
        break;
      }
    }
  };

  const expandAnswerForAudio = () => {
    if (!requestPayload || !api) return Promise.resolve(answerText);
    setAssistantStatus("Generazione della risposta…");
    return api
      .assistantChat({ ...requestPayload, responseFormat: "audio" })
      .then((response) => {
        const expanded = String(response.answer || "").trim();
        return response.needs_clarification || !expanded ? answerText : expanded;
      })
      .catch(() => answerText);
  };

  let audioRequestToken = 0;
  const requestAudio = () => {
    const token = ++audioRequestToken;
    contentNode.replaceChildren();
    const audioLoading = element("p", { className: "assistant-audio-loading" });
    const audioLoadingLabel = element("span", { className: "assistant-loading-dots", text: "Nuvia sta ampliando la risposta" });
    audioLoading.append(audioLoadingLabel);
    contentNode.append(audioLoading);
    scrollAssistantToLatest();

    let resolvedAudioAnswer = answerText;
    expandAnswerForAudio()
      .then((audioAnswer) => {
        if (token !== audioRequestToken) return Promise.reject(new Error("stale"));
        resolvedAudioAnswer = audioAnswer;
        if (audioAnswer !== answerText) reconcileHistoryWithAudioAnswer(audioAnswer);
        setAssistantStatus("Generazione audio in corso…");
        audioLoadingLabel.textContent = "Nuvia sta generando l'audio";
        return api.assistantSpeech(audioAnswer);
      })
      .then((response) => {
        if (token !== audioRequestToken) return;
        audioLoading.remove();
        bubble.classList.add("assistant-message-audio");
        const player = buildAssistantAudioPlayer(
          response.audio_base64,
          response.mime || "audio/wav",
          resolvedAudioAnswer,
          { onRegenerate: requestAudio },
        );
        contentNode.append(player);
        setAssistantStatus();
        scrollAssistantToLatest();
      })
      .catch((error) => {
        if (token !== audioRequestToken || String(error?.message) === "stale") return;
        audioLoading.remove();
        const errorWrapper = element("div", { className: "assistant-audio-player assistant-audio-player-error" });
        contentNode.append(errorWrapper);
        setAssistantAudioError(errorWrapper, messageFor(error), { onRegenerate: requestAudio });
        setAssistantStatus("Errore durante la generazione dell'audio.", { error: true });
        scrollAssistantToLatest();
      });
  };

  audioButton.addEventListener("click", () => {
    if (choiceMade) return;
    choiceMade = true;
    audioButton.classList.add("is-selected");
    setButtonBusy(audioButton, true);
    disableChoice();
    removeChoice();
    requestAudio();
  });

  return bubble;
}

function setAssistantStatus(message = "", { error = false } = {}) {
  if (!refs.assistantStatus) return;
  refs.assistantStatus.textContent = message;
  refs.assistantStatus.hidden = !message;
  refs.assistantStatus.classList.toggle("is-error", Boolean(error));
}

function updateAssistantControls() {
  if (!refs.assistantSend) return;
  const hasText = Boolean(String(refs.assistantInput?.value || "").trim());
  refs.assistantSend.disabled = assistantPending || (!hasText && !assistantAttachment);
}

function assistantFileExtension(filename) {
  const match = /\.[^.]+$/.exec(String(filename || ""));
  return match ? match[0].toLowerCase() : "";
}

function renderAssistantAttachmentPreview() {
  if (!refs.assistantAttachmentPreview) return;
  const hasAttachment = Boolean(assistantAttachment);
  refs.assistantAttachmentPreview.hidden = !hasAttachment;
  if (refs.assistantAttachmentName) {
    refs.assistantAttachmentName.textContent = hasAttachment ? assistantAttachment.filename : "";
  }
}

function clearAssistantAttachment() {
  assistantAttachment = null;
  if (refs.assistantAttachmentInput) refs.assistantAttachmentInput.value = "";
  renderAssistantAttachmentPreview();
  updateAssistantControls();
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Impossibile leggere il file selezionato."));
    reader.onload = () => {
      const result = String(reader.result || "");
      const commaIndex = result.indexOf(",");
      resolve(commaIndex >= 0 ? result.slice(commaIndex + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

async function handleAssistantAttachmentSelected(file) {
  if (!file) return;
  const mimeType = String(file.type || "").toLowerCase();
  const extension = assistantFileExtension(file.name);
  const mimeAllowed = ASSISTANT_ATTACHMENT_MIME_TYPES.has(mimeType);
  const extensionAllowed = ASSISTANT_ATTACHMENT_EXTENSIONS.has(extension);
  if (!mimeAllowed && !extensionAllowed) {
    setAssistantStatus("Formato non supportato. Usa JPEG, PNG, PDF o TXT.", { error: true });
    if (refs.assistantAttachmentInput) refs.assistantAttachmentInput.value = "";
    return;
  }
  if (file.size > ASSISTANT_ATTACHMENT_MAX_BYTES) {
    setAssistantStatus("Il file supera la dimensione massima di 8 MB.", { error: true });
    if (refs.assistantAttachmentInput) refs.assistantAttachmentInput.value = "";
    return;
  }
  try {
    const data = await readFileAsBase64(file);
    assistantAttachment = {
      filename: file.name || "allegato",
      mimeType: mimeType || (extension === ".pdf"
        ? "application/pdf"
        : extension === ".txt"
          ? "text/plain"
          : extension === ".png"
            ? "image/png"
            : "image/jpeg"),
      size: file.size,
      data,
    };
    setAssistantStatus();
    renderAssistantAttachmentPreview();
    updateAssistantControls();
    refs.assistantInput?.focus();
  } catch {
    setAssistantStatus("Impossibile leggere il file selezionato. Riprova.", { error: true });
    if (refs.assistantAttachmentInput) refs.assistantAttachmentInput.value = "";
  }
}

/** Renders the user's own recorded voice message as a chat bubble containing a
 *  playable audio control (same player component used for the assistant's audio
 *  answers), so it is displayed in the chat as audio content — never as text and
 *  never showing the internal transcription used to analyze it. */
function assistantVoiceUserMessage(audioBase64, mimeType) {
  const bubble = element("div", {
    className: "assistant-message assistant-message-user assistant-message-voice assistant-message-audio",
    attributes: { "aria-label": "Messaggio vocale inviato" },
  });
  const shell = element("div", { className: "assistant-message-shell" });
  const bubbleSurface = element("div", { className: "assistant-message-bubble" });
  const contentNode = element("div", { className: "assistant-message-content" });
  const player = buildAssistantAudioPlayer(audioBase64, mimeType || "audio/webm", "");
  contentNode.append(player);
  bubbleSurface.append(contentNode);
  shell.append(bubbleSurface);
  bubble.append(shell);
  refs.assistantMessageList?.append(bubble);
  scrollAssistantToLatest();
  return bubble;
}

function formatAssistantRecordingTime(elapsedMs) {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function readBlobAsBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Impossibile leggere la registrazione."));
    reader.onload = () => {
      const result = String(reader.result || "");
      const commaIndex = result.indexOf(",");
      resolve(commaIndex >= 0 ? result.slice(commaIndex + 1) : result);
    };
    reader.readAsDataURL(blob);
  });
}

function assistantSupportedRecordingMimeType() {
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return "";
  return ASSISTANT_RECORDING_MIME_CANDIDATES.find((candidate) => MediaRecorder.isTypeSupported(candidate)) || "";
}

function writeAsciiStringToView(view, offset, text) {
  for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
}

/** Encodes mono 32-bit float PCM samples as a standard 16-bit PCM WAV file. */
function encodeMonoPcm16Wav(samples, sampleRate) {
  const bytesPerSample = 2;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);
  writeAsciiStringToView(view, 0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeAsciiStringToView(view, 8, "WAVE");
  writeAsciiStringToView(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeAsciiStringToView(view, 36, "data");
  view.setUint32(40, dataSize, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

/** Converts a recorded voice message (typically audio/webm from MediaRecorder) into a
 *  mono 16 kHz WAV file. This is required because the transcription service only
 *  accepts WAV, MP3, AIFF, AAC, OGG or FLAC - not the WebM/Opus container that browsers
 *  produce by default - so every recording is normalized to WAV before upload,
 *  regardless of which browser or codec was used to capture it. */
async function convertRecordingToWav(blob, targetSampleRate = 16000) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) throw new Error("Conversione audio non supportata da questo browser.");

  const arrayBuffer = await blob.arrayBuffer();
  const decodingContext = new AudioContextClass();
  let decoded;
  try {
    decoded = await decodingContext.decodeAudioData(arrayBuffer);
  } finally {
    void decodingContext.close().catch(() => {});
  }

  const frameCount = Math.max(1, Math.ceil(decoded.duration * targetSampleRate));
  const offlineContext = new OfflineAudioContext(1, frameCount, targetSampleRate);
  const source = offlineContext.createBufferSource();
  source.buffer = decoded;
  source.connect(offlineContext.destination);
  source.start(0);
  const rendered = await offlineContext.startRendering();

  return encodeMonoPcm16Wav(rendered.getChannelData(0), rendered.sampleRate);
}

function setAssistantRecordingUiActive(active) {
  refs.assistantRecordingBar?.toggleAttribute("hidden", !active);
  refs.assistantRecordingBar?.classList.toggle("is-active", active);
  refs.assistantComposer?.classList.toggle("is-recording", active);
  refs.assistantRecordButton?.classList.toggle("is-recording", active);
  refs.assistantRecordButton?.setAttribute("aria-pressed", String(active));
  if (refs.assistantAttachmentButton) refs.assistantAttachmentButton.disabled = active;
  if (refs.assistantInput) refs.assistantInput.disabled = active;
}

function clearAssistantRecordingTimer() {
  if (assistantRecordingTimerHandle) {
    window.clearInterval(assistantRecordingTimerHandle);
    assistantRecordingTimerHandle = 0;
  }
}

/** Starts recording a voice message from the user's microphone. Any failure to access
 *  the microphone (permission denied, no device, unsupported browser) is surfaced as a
 *  clear inline status message rather than a silent no-op. */
async function startAssistantVoiceRecording() {
  if (assistantMediaRecorder || assistantPending) return;
  if (typeof MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia) {
    setAssistantStatus("Il browser non supporta la registrazione di messaggi vocali.", { error: true });
    return;
  }
  const mimeType = assistantSupportedRecordingMimeType();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    setAssistantStatus(
      "Impossibile accedere al microfono. Controlla i permessi del browser e riprova.",
      { error: true },
    );
    return;
  }

  assistantRecordingStream = stream;
  assistantRecordingChunks = [];
  assistantRecordingCancelled = false;
  assistantRecordingMimeType = mimeType;

  let recorder;
  try {
    recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
  } catch {
    stream.getTracks().forEach((track) => track.stop());
    assistantRecordingStream = null;
    setAssistantStatus("Impossibile avviare la registrazione audio su questo dispositivo.", { error: true });
    return;
  }

  assistantMediaRecorder = recorder;
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data && event.data.size > 0) assistantRecordingChunks.push(event.data);
  });
  recorder.addEventListener("error", () => {
    setAssistantStatus("Si è verificato un errore durante la registrazione. Riprova.", { error: true });
    void stopAssistantVoiceRecording({ cancelled: true });
  });
  recorder.addEventListener("stop", () => {
    const resolvers = assistantRecordingStopResolvers;
    assistantRecordingStopResolvers = null;
    assistantRecordingStream?.getTracks().forEach((track) => track.stop());
    assistantRecordingStream = null;
    assistantMediaRecorder = null;
    clearAssistantRecordingTimer();
    setAssistantRecordingUiActive(false);
    if (resolvers) resolvers.resolve();
  });

  recorder.start();
  assistantRecordingStartedAt = Date.now();
  setAssistantRecordingUiActive(true);
  if (refs.assistantRecordingTime) refs.assistantRecordingTime.textContent = "00:00";
  setAssistantStatus();
  clearAssistantRecordingTimer();
  assistantRecordingTimerHandle = window.setInterval(() => {
    const elapsed = Date.now() - assistantRecordingStartedAt;
    if (refs.assistantRecordingTime) {
      refs.assistantRecordingTime.textContent = formatAssistantRecordingTime(elapsed);
    }
    if (elapsed >= ASSISTANT_AUDIO_MAX_DURATION_MS) void stopAssistantVoiceRecording({ cancelled: false });
  }, 250);
}

/** Stops the active recording (if any) and, unless cancelled, hands the resulting
 *  audio blob to the transcribe → analyze pipeline. Resolves once the recorder has
 *  fully stopped, so callers can safely start a new recording right after. */
async function stopAssistantVoiceRecording({ cancelled }) {
  const recorder = assistantMediaRecorder;
  if (!recorder) return;
  assistantRecordingCancelled = Boolean(cancelled);
  const stopped = new Promise((resolve) => {
    assistantRecordingStopResolvers = { resolve };
  });
  if (recorder.state !== "inactive") recorder.stop();
  else assistantRecordingStopResolvers?.resolve();
  await stopped;

  if (assistantRecordingCancelled) {
    assistantRecordingChunks = [];
    setAssistantStatus();
    return;
  }

  const chunks = assistantRecordingChunks;
  assistantRecordingChunks = [];
  const mimeType = assistantRecordingMimeType || chunks[0]?.type || "audio/webm";
  const blob = new Blob(chunks, { type: mimeType });
  await handleAssistantVoiceMessage(blob, mimeType);
}

/** Full voice-message pipeline, executed automatically once recording stops:
 *  1) show the recorded audio in the chat as the user's message ("Audio ricevuto");
 *  2) upload it for transcription ("Elaborazione in corso…") - the transcript itself
 *     is kept only in memory and never rendered;
 *  3) forward that transcript, exactly like a typed message, to the normal assistant
 *     endpoint ("Analisi del messaggio…"), which applies the very same parameter
 *     checks, instructions, memory, conversation context and safety/pertinence rules
 *     used for text messages - including the standard off-topic answer when relevant.
 *  Any failure at any step (empty recording, unsupported format, upload/transcription
 *  error) is surfaced with a clear, specific message instead of a silent failure. */
async function handleAssistantVoiceMessage(blob, mimeType) {
  if (assistantPending || !api) return;
  if (!blob || blob.size === 0) {
    setAssistantStatus("La registrazione è vuota. Riprova a registrare il messaggio.", { error: true });
    return;
  }

  const requestVersion = ++assistantRequestVersion;
  setAssistantStatus("Audio ricevuto…");

  // Normalize to WAV first (see convertRecordingToWav): the transcription service does
  // not accept the WebM/Opus container most browsers record by default.
  let wavBlob;
  try {
    wavBlob = await convertRecordingToWav(blob);
  } catch {
    setAssistantStatus("Impossibile elaborare la registrazione audio. Riprova.", { error: true });
    return;
  }
  if (requestVersion !== assistantRequestVersion) return;
  if (wavBlob.size > ASSISTANT_AUDIO_MAX_BYTES) {
    setAssistantStatus("Il messaggio vocale è troppo lungo. Registra un audio più breve.", { error: true });
    return;
  }

  let audioBase64;
  try {
    audioBase64 = await readBlobAsBase64(wavBlob);
  } catch {
    setAssistantStatus("Impossibile leggere la registrazione. Riprova.", { error: true });
    return;
  }
  if (requestVersion !== assistantRequestVersion) return;

  assistantVoiceUserMessage(audioBase64, "audio/wav");
  setAssistantPending(true);

  let transcript = "";
  try {
    setAssistantStatus("Elaborazione in corso…");
    const transcribeResponse = await api.assistantTranscribe({
      filename: "messaggio-vocale.wav",
      mimeType: "audio/wav",
      data: audioBase64,
    });
    if (requestVersion !== assistantRequestVersion) return;
    transcript = String(transcribeResponse.transcript || "").trim();
    if (!transcript) throw new ApiError("Non sono riuscita a comprendere il messaggio vocale. Riprova.");
  } catch (error) {
    if (requestVersion !== assistantRequestVersion) return;
    assistantMessage("assistant", messageFor(error), { error: true });
    setAssistantStatus("Trascrizione non riuscita. Puoi registrare di nuovo il messaggio.", { error: true });
    setAssistantPending(false);
    return;
  }

  setAssistantStatus("Analisi del messaggio…");
  const requestHistory = assistantHistory.slice(-12).map((item) => ({ ...item }));
  const requestContext = assistantActiveContext ? { ...assistantActiveContext } : {};
  const comparisonContext = ASSISTANT_COMPARISON_PATTERN.test(transcript) && assistantPreviousContext
    ? { ...assistantPreviousContext }
    : null;
  const loadingMessage = assistantMessage("assistant", "Nuvia sta scrivendo", { loading: true });
  try {
    const response = await api.assistantChat({
      message: transcript,
      context: requestContext,
      comparisonContext,
      history: requestHistory,
    });
    if (requestVersion !== assistantRequestVersion) return;
    setAssistantStatus("Nuvia sta elaborando la risposta…");
    loadingMessage?.remove();
    const answer = String(response.answer || "").trim();
    if (!answer) throw new ApiError("Nuvia non ha restituito una risposta. Riprova.");
    // The transcript, kept only in memory (never rendered), is associated with this
    // same turn in the conversation history so follow-up questions retain full context,
    // exactly as they would for a typed message.
    assistantHistory.push(
      { role: "user", content: transcript },
      { role: "assistant", content: answer },
    );
    assistantHistory = assistantHistory.slice(-12);
    if (response.needs_clarification || response.off_topic) {
      // Out-of-scope requests always receive the exact fallback as plain text.
      // Do not display the text/audio format selector and never synthesize it.
      assistantMessage("assistant", answer);
      setAssistantStatus();
    } else {
      assistantFormatChoiceMessage(answer, {
        message: transcript,
        context: requestContext,
        comparisonContext,
        history: requestHistory,
      });
      setAssistantStatus();
    }
  } catch (error) {
    if (requestVersion !== assistantRequestVersion) return;
    loadingMessage?.remove();
    assistantMessage("assistant", messageFor(error), { error: true });
    setAssistantStatus("Invio non riuscito. Puoi registrare di nuovo il messaggio.", { error: true });
  } finally {
    if (requestVersion === assistantRequestVersion) setAssistantPending(false);
  }
}

function assistantRecordingExtensionFor(mimeType) {
  const normalized = String(mimeType || "").toLowerCase();
  if (normalized.includes("ogg")) return ".ogg";
  if (normalized.includes("mp4")) return ".m4a";
  return ".webm";
}

function setupAssistantVoiceRecording() {
  refs.assistantRecordButton?.addEventListener("click", () => {
    if (assistantMediaRecorder) void stopAssistantVoiceRecording({ cancelled: false });
    else void startAssistantVoiceRecording();
  });
  refs.assistantRecordingStop?.addEventListener("click", () => {
    void stopAssistantVoiceRecording({ cancelled: false });
  });
  refs.assistantRecordingCancel?.addEventListener("click", () => {
    void stopAssistantVoiceRecording({ cancelled: true });
  });
}

let assistantDragDepth = 0;

function setAssistantDropActive(active) {
  refs.assistantDropZone?.classList.toggle("is-drag-active", active);
  if (refs.assistantDropOverlay) refs.assistantDropOverlay.hidden = !active;
}

function assistantDragEventHasFiles(event) {
  const types = event.dataTransfer?.types;
  if (!types) return false;
  return Array.from(types).includes("Files");
}

function setupAssistantDropZone() {
  const zone = refs.assistantDropZone;
  if (!zone) return;

  zone.addEventListener("dragenter", (event) => {
    if (!assistantDragEventHasFiles(event)) return;
    event.preventDefault();
    assistantDragDepth += 1;
    setAssistantDropActive(true);
  });

  zone.addEventListener("dragover", (event) => {
    if (!assistantDragEventHasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });

  zone.addEventListener("dragleave", (event) => {
    if (!assistantDragEventHasFiles(event)) return;
    event.preventDefault();
    assistantDragDepth = Math.max(0, assistantDragDepth - 1);
    if (assistantDragDepth === 0) setAssistantDropActive(false);
  });

  zone.addEventListener("drop", (event) => {
    if (!assistantDragEventHasFiles(event)) return;
    event.preventDefault();
    assistantDragDepth = 0;
    setAssistantDropActive(false);
    // Solo il primo file trascinato viene accettato; i formati non autorizzati
    // vengono comunque respinti da handleAssistantAttachmentSelected, che
    // applica la stessa validazione MIME/estensione/dimensione usata per la
    // selezione manuale del file.
    const file = event.dataTransfer?.files?.[0] || null;
    void handleAssistantAttachmentSelected(file);
  });
}



function setAssistantPending(pending) {
  assistantPending = Boolean(pending);
  refs.assistantComposer?.setAttribute("aria-busy", String(assistantPending));
  refs.assistantMessageList?.setAttribute("aria-busy", String(assistantPending));
  updateAssistantControls();
}

function renderAssistantContext(context) {
  if (!refs.assistantContext) return;
  refs.assistantContext.hidden = !context;
  if (!context) return;
  if (refs.assistantContextTitle) refs.assistantContextTitle.textContent = context.title || "Notizia senza titolo";
  if (!refs.assistantContextLink) return;
  if (context.url) {
    refs.assistantContextLink.href = context.url;
    refs.assistantContextLink.removeAttribute("aria-disabled");
    refs.assistantContextLink.textContent = context.source
      ? `${context.source} · ${context.url}`
      : context.url;
  } else {
    refs.assistantContextLink.removeAttribute("href");
    refs.assistantContextLink.setAttribute("aria-disabled", "true");
    refs.assistantContextLink.textContent = context.source || "Link della fonte non disponibile";
  }
}

function activateAssistantContext(item, { persist = true } = {}) {
  const context = normalizeAssistantContext(item);
  const previousContext = assistantActiveContext;
  const contextChanged = context && previousContext
    && `${context.id}|${context.title}|${context.url}` !== `${previousContext.id}|${previousContext.title}|${previousContext.url}`;
  if (contextChanged) assistantPreviousContext = { ...previousContext };
  if (!context) assistantPreviousContext = null;
  assistantRequestVersion += 1;
  assistantActiveContext = context;
  assistantHistory = [];
  setAssistantPending(false);
  setAssistantStatus();
  if (refs.assistantInput) refs.assistantInput.value = "";
  clearAssistantAttachment();
  refs.assistantMessageList?.replaceChildren();
  renderAssistantContext(context);
  if (context) {
    assistantHistory = [{ role: "assistant", content: ASSISTANT_CONTEXT_PROMPT }];
    assistantMessage("assistant", ASSISTANT_CONTEXT_PROMPT);
  } else {
    assistantMessage("assistant", "Ciao, sono Nuvia!\nQuale notizia vorresti approfondire oggi?");
  }
  if (persist) persistAssistantContext(context);
  updateAssistantControls();
}

function openAssistantForNews(item) {
  const context = normalizeAssistantContext(item);
  if (!context) {
    showAppFeedback("Non ci sono contenuti sufficienti per approfondire questa notizia.", { error: true });
    return;
  }
  activateAssistantContext(context);
  if (location.pathname !== "/assistant") window.history.pushState({}, "", "/assistant");
  void syncLocationState();
  window.requestAnimationFrame(() => refs.assistantInput?.focus());
}

async function submitAssistantMessage() {
  const typedMessage = String(refs.assistantInput?.value || "").trim();
  const attachment = assistantAttachment;
  if ((!typedMessage && !attachment) || assistantPending || !api) return;

  if (attachment && !typedMessage) {
    assistantMessage("user", `📎 ${attachment.filename}`);
    assistantMessage("assistant", ASSISTANT_ATTACHMENT_CLARIFY_QUESTION);
    assistantHistory.push(
      { role: "user", content: `📎 ${attachment.filename}` },
      { role: "assistant", content: ASSISTANT_ATTACHMENT_CLARIFY_QUESTION },
    );
    assistantHistory = assistantHistory.slice(-12);
    if (refs.assistantInput) refs.assistantInput.value = "";
    updateAssistantControls();
    refs.assistantInput?.focus();
    return;
  }

  const message = typedMessage || ASSISTANT_DEFAULT_ATTACHMENT_MESSAGE;
  const requestHistory = assistantHistory.slice(-12).map((item) => ({ ...item }));
  const requestContext = assistantActiveContext ? { ...assistantActiveContext } : {};
  const comparisonContext = ASSISTANT_COMPARISON_PATTERN.test(message) && assistantPreviousContext
    ? { ...assistantPreviousContext }
    : null;
  const requestVersion = ++assistantRequestVersion;
  assistantMessage(
    "user",
    attachment ? `${message}\n\n📎 ${attachment.filename}` : message,
  );
  if (refs.assistantInput) refs.assistantInput.value = "";
  clearAssistantAttachment();
  setAssistantPending(true);
  setAssistantStatus(
    attachment ? "Nuvia sta analizzando il file allegato…" : "Verifica delle informazioni…",
  );
  const loadingMessage = assistantMessage("assistant", "Nuvia sta scrivendo", { loading: true });
  try {
    const response = await api.assistantChat({
      message,
      context: requestContext,
      comparisonContext,
      history: requestHistory,
      attachment: attachment
        ? { filename: attachment.filename, mimeType: attachment.mimeType, data: attachment.data }
        : null,
    });
    if (requestVersion !== assistantRequestVersion) return;
    setAssistantStatus("Nuvia sta elaborando la risposta…");
    loadingMessage?.remove();
    const answer = String(response.answer || "").trim();
    if (!answer) throw new ApiError("Nuvia non ha restituito una risposta. Riprova.");
    assistantHistory.push(
      { role: "user", content: message },
      { role: "assistant", content: answer },
    );
    assistantHistory = assistantHistory.slice(-12);
    if (response.needs_clarification || response.off_topic) {
      // Out-of-scope requests always receive the exact fallback as plain text.
      // Do not display the text/audio format selector and never synthesize it.
      assistantMessage("assistant", answer);
      setAssistantStatus();
    } else {
      assistantFormatChoiceMessage(answer, {
        message,
        context: requestContext,
        comparisonContext,
        history: requestHistory,
      });
      setAssistantStatus();
    }
  } catch (error) {
    if (requestVersion !== assistantRequestVersion) return;
    loadingMessage?.remove();
    assistantMessage("assistant", messageFor(error), { error: true });
    setAssistantStatus("Invio non riuscito. La barra è pronta per riprovare.", { error: true });
  } finally {
    if (requestVersion === assistantRequestVersion) {
      setAssistantPending(false);
      refs.assistantInput?.focus();
    }
  }
}

function initializeAssistant() {
  activateAssistantContext(storedAssistantContext(), { persist: false });
  refs.assistantContextClose?.addEventListener("click", () => {
    activateAssistantContext(null);
    refs.assistantInput?.focus();
  });
  refs.assistantAttachmentButton?.addEventListener("click", () => {
    refs.assistantAttachmentInput?.click();
  });
  refs.assistantAttachmentInput?.addEventListener("change", (event) => {
    const file = event.target.files?.[0] || null;
    void handleAssistantAttachmentSelected(file);
  });
  refs.assistantAttachmentRemove?.addEventListener("click", () => {
    clearAssistantAttachment();
    refs.assistantInput?.focus();
  });
  setupAssistantDropZone();
  setupAssistantVoiceRecording();
  refs.assistantInput?.addEventListener("input", updateAssistantControls);
  refs.assistantInput?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    if (!refs.assistantSend?.disabled) refs.assistantComposer?.requestSubmit();
  });
  refs.assistantComposer?.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitAssistantMessage();
  });
}

function setBackgroundInert(inert) {
  [refs.sidebar, refs.canvasContainer].filter(Boolean).forEach((node) => {
    node.inert = inert;
    node.setAttribute("aria-hidden", String(inert));
  });
}

function focusableNodes(root) {
  return [...root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((node) => !node.hidden && node.getClientRects().length);
}

function focusTrap(event) {
  if (event.key !== "Tab") return;
  const modal = refs.authPage?.getAttribute("aria-hidden") === "false"
    ? refs.authPage
    : (!refs.settingsPage?.classList.contains("hidden") ? refs.settingsPage : null);
  if (!modal) return;
  const nodes = focusableNodes(modal);
  if (!nodes.length) return;
  const first = nodes[0];
  const last = nodes.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function setAuthMode(mode) {
  const normalized = mode === "register" ? "register" : "login";
  store.set({ authMode: normalized });
  const isLogin = normalized === "login";
  if (refs.authLogoLink) {
    const destination = isLogin ? "Login" : "Registrazione";
    refs.authLogoLink.href = isLogin ? "/login" : "/register";
    refs.authLogoLink.title = destination;
    refs.authLogoLink.setAttribute("aria-label", destination);
  }
  refs.authPage?.classList.toggle("is-login", isLogin);
  refs.authPage?.querySelectorAll("[data-register-only]").forEach((node) => { node.hidden = isLogin; });
  document.querySelectorAll("[data-auth-mode]").forEach((button) => {
    const active = button.getAttribute("data-auth-mode") === normalized;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.getElementById("auth-form-panel")?.setAttribute(
    "aria-labelledby",
    isLogin ? "auth-login-tab" : "auth-register-tab",
  );
  const password = query('[data-auth-field="password"]');
  const confirmation = query('[data-auth-field="confirm-password"]');
  if (password) password.autocomplete = isLogin ? "current-password" : "new-password";
  if (confirmation) confirmation.required = !isLogin;
  const submit = query("[data-auth-submit]");
  if (submit) submit.textContent = isLogin ? "Accedi" : "Crea account";
  refs.authFeedback && (refs.authFeedback.textContent = "");
}

function showAuth(mode = "login", message = "") {
  closeSettings({ restoreFocus: false });
  setAuthMode(mode);
  if (!refs.authPage) return;
  refs.authPage.setAttribute("aria-hidden", "false");
  document.body.classList.add("auth-open");
  setBackgroundInert(true);
  if (message) refs.authFeedback.textContent = message;
  window.requestAnimationFrame(() => query('[data-auth-field="email"]')?.focus());
}

function hideAuth() {
  refs.authPage?.setAttribute("aria-hidden", "true");
  document.body.classList.remove("auth-open");
  if (refs.settingsPage?.classList.contains("hidden")) setBackgroundInert(false);
}

function closeSettings({ restoreFocus = true } = {}) {
  if (!refs.settingsPage || refs.settingsPage.classList.contains("hidden")) return;
  refs.settingsPage.classList.add("hidden");
  refs.settingsPage.setAttribute("aria-hidden", "true");
  document.body.classList.remove("settings-open");
  setBackgroundInert(false);
  setSidebarActive(sidebarSectionBeforeSettings || "home");
  sidebarSectionBeforeSettings = "";
  if (restoreFocus && modalReturnFocus?.focus) modalReturnFocus.focus();
  modalReturnFocus = null;
}

async function openSettings(trigger = document.activeElement) {
  if (!store.get().user) {
    showAuth("login", "Accedi per modificare le impostazioni.");
    return;
  }
  if (!refs.settingsPage) return;
  sidebarSectionBeforeSettings = activeSidebarSection || "home";
  setSidebarActive("settings");
  modalReturnFocus = trigger;
  refs.settingsPage.classList.remove("hidden");
  refs.settingsPage.setAttribute("aria-hidden", "false");
  document.body.classList.add("settings-open");
  setBackgroundInert(true);
  window.requestAnimationFrame(() => refs.settingsTitle?.focus());
  showLiveMessage(refs.settingsFeedback, "Caricamento impostazioni…");
  try {
    const response = await api.settings();
    hydrateSettings(response.settings);
    showLiveMessage(refs.settingsFeedback, "");
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  }
}

function hydrateSettings(settings) {
  if (!settings || typeof settings !== "object") return;
  const profile = settings.profile || {};
  const preferences = settings.preferences || {};
  const suspension = settings.suspension || {};
  const telegram = settings.telegram || {};
  const push = settings.push || {};
  const currentUser = store.get().user;
  store.set({
    settings,
    user: currentUser
      ? {
        ...currentUser,
        email: profile.email || currentUser.email,
        first_name: profile.first_name || currentUser.first_name,
        last_name: profile.last_name || currentUser.last_name,
        preferences: { ...(currentUser.preferences || {}), ...preferences },
      }
      : currentUser,
  });
  const email = query("#settings-profile-email");
  const newsletterEmail = query("#settings-subscription-email");
  const emailChannel = query("#settings-channel-email");
  if (email) email.value = profile.email || "";
  if (newsletterEmail) newsletterEmail.value = profile.email || "";
  if (emailChannel) emailChannel.checked = Boolean(preferences.email);
  const telegramStatus = query('[data-ui="telegram-status"]');
  const telegramOpen = query('[data-ui="telegram-open"]');
  const telegramOpenLabel = telegramOpen ? query("span:last-child", telegramOpen) : null;
  if (telegram.connected) {
    if (telegramStatus) telegramStatus.textContent = "Telegram è collegato a questo account.";
    if (telegramOpen) {
      telegramOpen.disabled = true;
      telegramOpen.setAttribute("aria-disabled", "true");
      if (telegramOpenLabel) telegramOpenLabel.textContent = "Telegram collegato";
    }
  } else if (!telegram.configured) {
    if (telegramStatus) telegramStatus.textContent = "Telegram non è configurato in questo ambiente.";
    if (telegramOpen) {
      telegramOpen.disabled = true;
      telegramOpen.setAttribute("aria-disabled", "true");
      if (telegramOpenLabel) telegramOpenLabel.textContent = "Apri Telegram";
    }
  } else {
    if (telegramStatus) telegramStatus.textContent = "Apri Telegram e premi Start: la connessione verrà verificata dal server.";
    if (telegramOpen) {
      telegramOpen.disabled = false;
      telegramOpen.setAttribute("aria-disabled", "false");
      if (telegramOpenLabel) telegramOpenLabel.textContent = "Apri Telegram";
    }
  }

  const start = query("#settings-pause-start");
  const end = query("#settings-pause-end");
  const status = query('[data-ui="suspension-status"]');
  const resume = query('[data-ui="suspension-resume"]');
  if (start) { start.value = suspension.start || ""; start.min = todayIso(); }
  if (end) { end.value = suspension.end || ""; end.min = start?.value || todayIso(); }
  if (status) {
    status.textContent = suspension.active
      ? "Attiva"
      : (suspension.configured ? "Programmata" : "Non attiva");
  }
  if (resume) resume.textContent = suspension.end
    ? `Automatica il ${formatDate(suspension.end)} (${suspension.timezone || "UTC"})`
    : "Automatica al termine del periodo";

  const pushStatus = query('[data-ui="push-status"]');
  const pushButton = query('[data-settings-action="toggle-push"]');
  const pushNote = query('[data-ui="push-note"]');
  const browserSupported = "Notification" in window && "serviceWorker" in navigator;
  if (!push.configured) {
    if (pushStatus) pushStatus.textContent = "Le notifiche push non sono disponibili in questo ambiente.";
    if (pushNote) pushNote.textContent = "La disponibilità dipende dalla configurazione del server.";
  } else if (!browserSupported) {
    if (pushStatus) pushStatus.textContent = "Questo browser non supporta le notifiche push.";
    if (pushNote) pushNote.textContent = "Usa un browser compatibile per attivare le notifiche.";
  } else {
    const permission = Notification.permission;
    if (pushStatus) pushStatus.textContent = permission === "denied"
      ? "Permesso notifiche negato dal browser."
      : `Permesso browser: ${permission === "granted" ? "concesso" : "da richiedere"}.`;
    if (pushNote) pushNote.textContent = "L'attivazione registra questo dispositivo nel servizio push configurato.";
  }
  if (pushButton) {
    const disabled = !push.configured || !browserSupported || Notification.permission === "denied";
    pushButton.disabled = disabled;
    pushButton.setAttribute("aria-disabled", String(disabled));
  }
  void syncPushControl(push);
}

function setPushControlState(button, active) {
  if (!button) return;
  button.setAttribute("aria-pressed", String(active));
  const label = query("span:last-child", button);
  if (label) label.textContent = active ? "Disattiva notifiche push" : "Attiva notifiche push";
}

async function syncPushControl(pushSettings) {
  const button = query('[data-settings-action="toggle-push"]');
  if (!button || !pushSettings?.configured || !("Notification" in window) || Notification.permission !== "granted") {
    setPushControlState(button, false);
    return;
  }
  try {
    const runtime = await firebaseClient();
    const token = await runtime.messagingModule.getToken(runtime.messaging, {
      vapidKey: runtime.config.vapidKey,
      serviceWorkerRegistration: runtime.registration,
    });
    if (!token) {
      pushRuntime.token = null;
      setPushControlState(button, false);
      return;
    }
    const status = await api.pushStatus(token);
    // Browser permission is device-wide; only a token registered to the
    // current signed-in account counts as an active account notification.
    pushRuntime.token = status.registered ? token : null;
    setPushControlState(button, Boolean(status.registered));
  } catch {
    // Keep the capability state visible even if token discovery is temporarily unavailable.
    setPushControlState(button, false);
  }
}

function messageFor(error) {
  return error instanceof ApiError ? error.message : "Si è verificato un errore inatteso.";
}

async function loadCurrentDigest() {
  refs.digestList?.setAttribute("aria-busy", "true");
  hideDigestState();
  try {
    const response = await api.currentDigest();
    const digest = response.digest || { items: [] };
    store.set({
      digest,
      visible: {
        kind: "current",
        items: normalizeItems(digest.items),
        digestDate: digest.digest_date || "",
        weekStart: digest.week_start || "",
        weekEnd: digest.week_end || "",
      },
    });
    document.title = "Nuvia.AI – Homepage";
    renderVisibleDigest();
  } catch (error) {
    if (error.status === 401) {
      refs.digestList?.setAttribute("aria-busy", "false");
      return;
    }
    refs.digestList?.replaceChildren();
    refs.digestList?.setAttribute("aria-busy", "false");
    renderDigestState(refs.digestState, { kind: "error", message: messageFor(error), retry: true });
  }
}

async function loadSaved() {
  refs.savedList?.setAttribute("aria-busy", "true");
  try {
    const response = await api.saved();
    store.set({ saved: normalizeItems(response.items) });
    renderSavedNews();
  } catch (error) {
    if (error.status === 401) {
      refs.savedList?.setAttribute("aria-busy", "false");
      return;
    }
    if (refs.savedState) {
      refs.savedState.hidden = false;
      refs.savedState.textContent = `${messageFor(error)} Riprova più tardi.`;
    }
    refs.savedList?.setAttribute("aria-busy", "false");
  }
}

async function loadHistory({ reset = true } = {}) {
  const state = store.get();
  const queryValue = String(refs.historySearch?.value || "").trim();
  const offset = reset ? 0 : (state.historyNextOffset ?? 0);
  if (!reset && state.historyNextOffset === null) return;
  const requestVersion = ++historyRequestVersion;
  historyAbortController?.abort();
  const controller = new AbortController();
  historyAbortController = controller;
  refs.historyList?.setAttribute("aria-busy", "true");
  if (refs.historyState) {
    refs.historyState.hidden = false;
    refs.historyState.textContent = reset ? "Caricamento cronologia…" : "Caricamento altri digest…";
  }
  try {
    const response = await api.history({ q: queryValue, offset, limit: 12, signal: controller.signal });
    if (requestVersion !== historyRequestVersion) return;
    const incoming = normalizeItems(response.items);
    const records = reset ? incoming : [...state.history, ...incoming];
    const unique = [...new Map(records.map((item) => [item.id, item])).values()];
    store.set({
      history: unique,
      historyNextOffset: response.next_offset ?? null,
      historyQuery: queryValue,
    });
    renderHistoryList();
  } catch (error) {
    if (requestVersion !== historyRequestVersion) return;
    if (error.status === 401) {
      refs.historyList?.setAttribute("aria-busy", "false");
      return;
    }
    if (refs.historyState) {
      refs.historyState.hidden = false;
      refs.historyState.textContent = `${messageFor(error)} Riprova la ricerca.`;
    }
    refs.historyList?.setAttribute("aria-busy", "false");
  } finally {
    if (requestVersion === historyRequestVersion) historyAbortController = null;
  }
}

async function loadHistoricalDigest(selection, { focus = false } = {}) {
  if (!selection?.id && !selection?.date) return false;
  refs.digestList?.setAttribute("aria-busy", "true");
  hideDigestState();
  try {
    const response = await api.digest(selection);
    const digest = response.digest || { items: [] };
    store.set({
      visible: {
        kind: "history",
        items: normalizeItems(digest.items),
        title: digest.title,
        digestDate: digest.digest_date || "",
        weekStart: digest.week_start || "",
        weekEnd: digest.week_end || "",
      },
    });
    document.title = "Nuvia.AI – Homepage";
    renderVisibleDigest();
    if (focus) refs.digestList?.querySelector(".info-card")?.focus({ preventScroll: false });
    return true;
  } catch (error) {
    refs.digestList?.setAttribute("aria-busy", "false");
    renderDigestState(refs.digestState, { kind: "error", message: messageFor(error), retry: true });
    return false;
  }
}

function openSavedNews(item) {
  if (!item) return;
  store.set({
    visible: {
      kind: "saved",
      items: [item],
      title: item.title || "Notizia salvata",
      digestDate: item.digest_date || "",
    },
  });
  renderVisibleDigest();
  refs.digestList?.querySelector(".info-card")?.focus({ preventScroll: false });
}

function updateSavedState(newsId, saved, fallbackItem) {
  const state = store.get();
  const savedItems = saved
    ? [
      { ...(fallbackItem || {}), id: newsId, saved: true },
      ...state.saved.filter((item) => item.id !== newsId),
    ]
    : state.saved.filter((item) => item.id !== newsId);
  const visible = { ...state.visible, items: replaceSaveState(state.visible.items, newsId, saved) };
  const digest = state.digest
    ? { ...state.digest, items: replaceSaveState(state.digest.items, newsId, saved) }
    : null;
  store.set({ saved: savedItems, visible, digest });
  renderVisibleDigest();
  renderSavedNews();
}

async function toggleSavedNews(item) {
  const newsId = String(item?.id || "");
  if (!newsId || saveRequests.has(newsId)) return;
  const nextSaved = !item.saved;
  saveRequests.add(newsId);
  updateSavedState(newsId, nextSaved, item);
  try {
    if (nextSaved) {
      const response = await api.saveNews(newsId);
      if (response.item) updateSavedState(newsId, true, response.item);
    } else {
      await api.removeNews(newsId);
    }
  } catch (error) {
    updateSavedState(newsId, !nextSaved, item);
    showAppFeedback(messageFor(error), { error: true });
  } finally {
    saveRequests.delete(newsId);
  }
}

async function saveProfile(button) {
  const input = query("#settings-profile-email");
  if (!input?.checkValidity()) {
    input?.focus();
    showLiveMessage(refs.settingsFeedback, "Inserisci un indirizzo email valido.", { error: true });
    return;
  }
  setButtonBusy(button, true, "Salvataggio…");
  try {
    const response = await api.updateProfile(input.value.trim());
    hydrateSettings(response.settings);
    showLiveMessage(refs.settingsFeedback, response.message || "Email aggiornata.");
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    setButtonBusy(button, false, "Salva email");
  }
}

async function savePreferences(button) {
  const email = Boolean(query("#settings-channel-email")?.checked);
  const telegram = Boolean(store.get().settings?.preferences?.telegram);
  setButtonBusy(button, true, "Salvataggio…");
  try {
    const response = await api.updatePreferences({ email, telegram });
    hydrateSettings(response.settings);
    showLiveMessage(refs.settingsFeedback, response.message || "Preferenze aggiornate.");
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    setButtonBusy(button, false, "Salva preferenze");
  }
}

async function sendNewsletter(button) {
  const email = query("#settings-subscription-email");
  if (!email?.checkValidity()) {
    email?.focus();
    showLiveMessage(refs.settingsFeedback, "Inserisci un indirizzo email valido.", { error: true });
    return;
  }
  setButtonBusy(button, true, "Invio in corso…");
  try {
    const response = await api.newsletter(email.value.trim());
    showLiveMessage(refs.settingsFeedback, response.message || "Digest inviato.");
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    setButtonBusy(button, false, "Invia digest");
  }
}

async function openTelegram(button) {
  // Open synchronously while the click still has user activation; the server
  // request below would otherwise make most browsers block the new tab.
  const popup = window.open("", "_blank");
  if (!popup) {
    showLiveMessage(refs.settingsFeedback, "Il browser ha bloccato l'apertura di Telegram.", { error: true });
    return;
  }
  setButtonBusy(button, true, "Apertura…");
  try {
    const response = await api.telegramOnboarding();
    const telegram = response.telegram || {};
    const onboardingUrl = safeExternalUrl(telegram.onboarding_url);
    if (!telegram.configured || telegram.connected || !onboardingUrl) {
      throw new ApiError("Telegram non è disponibile per questo account.");
    }
    // The page starts same-origin, so sever the opener before it navigates to
    // the external Telegram URL.
    popup.opener = null;
    popup.location.replace(onboardingUrl);
    showLiveMessage(refs.settingsFeedback, "Telegram è stato aperto. Premi Start nel bot: la connessione verrà verificata dal server.");
    window.setTimeout(() => void refreshSettingsSilently(), 4_000);
  } catch (error) {
    popup.close();
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    setButtonBusy(button, false, "Apri Telegram");
  }
}

async function refreshSettingsSilently() {
  if (refs.settingsPage?.classList.contains("hidden")) return;
  try {
    const response = await api.settings();
    hydrateSettings(response.settings);
  } catch {
    // The dialog retains its last confirmed state while the network is unavailable.
  }
}

async function saveSuspension(button) {
  const start = query("#settings-pause-start");
  const end = query("#settings-pause-end");
  const startValue = start?.value || "";
  const endValue = end?.value || "";
  if (!startValue || !endValue || endValue < startValue) {
    showLiveMessage(refs.settingsFeedback, "Seleziona una data di inizio e una di fine valida.", { error: true });
    (startValue ? end : start)?.focus();
    return;
  }
  setButtonBusy(button, true, "Salvataggio…");
  try {
    const response = await api.updateSuspension({ action: "save", start_date: startValue, end_date: endValue });
    const settings = store.get().settings || {};
    hydrateSettings({ ...settings, suspension: response.suspension });
    showLiveMessage(refs.settingsFeedback, response.message || "Sospensione aggiornata.");
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    setButtonBusy(button, false, "Salva sospensione");
  }
}

async function clearSuspension(button) {
  setButtonBusy(button, true, "Annullamento…");
  try {
    const response = await api.updateSuspension({ action: "clear" });
    const settings = store.get().settings || {};
    hydrateSettings({ ...settings, suspension: response.suspension });
    showLiveMessage(refs.settingsFeedback, response.message || "Sospensione annullata.");
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    setButtonBusy(button, false, "Annulla sospensione");
  }
}

async function firebaseClient() {
  if (pushRuntime) return pushRuntime;
  const config = await api.firebaseConfig();
  if (!config.configured || !config.web || !config.vapidKey) {
    throw new ApiError("Le notifiche push non sono configurate per questo ambiente.");
  }
  if (!("serviceWorker" in navigator) || !("Notification" in window)) {
    throw new ApiError("Questo browser non supporta le notifiche push.");
  }
  const version = String(config.sdkVersion || "");
  if (!/^\d+(?:\.\d+){1,3}$/.test(version)) throw new ApiError("Configurazione push non valida.");
  const serviceWorkerUrl = new URL(String(config.serviceWorkerUrl || ""), window.location.origin);
  if (serviceWorkerUrl.origin !== window.location.origin) throw new ApiError("Configurazione push non valida.");
  const [appModule, messagingModule] = await Promise.all([
    import(`https://www.gstatic.com/firebasejs/${version}/firebase-app.js`),
    import(`https://www.gstatic.com/firebasejs/${version}/firebase-messaging.js`),
  ]);
  const registration = await navigator.serviceWorker.register(serviceWorkerUrl.href);
  const app = appModule.initializeApp(config.web);
  const messaging = messagingModule.getMessaging(app);
  pushRuntime = { config, registration, messaging, messagingModule };
  return pushRuntime;
}

async function togglePush(button) {
  const runtime = await firebaseClient().catch((error) => {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
    return null;
  });
  if (!runtime) return;
  setButtonBusy(button, true, "Verifica…");
  try {
    if (button.getAttribute("aria-pressed") === "true") {
      const token = pushRuntime.token || await runtime.messagingModule.getToken(runtime.messaging, {
        vapidKey: runtime.config.vapidKey,
        serviceWorkerRegistration: runtime.registration,
      });
      if (token) {
        await api.unsubscribePush(token);
        await runtime.messagingModule.deleteToken(runtime.messaging);
      }
      pushRuntime.token = null;
      setPushControlState(button, false);
      showLiveMessage(refs.settingsFeedback, "Notifiche push disattivate su questo dispositivo.");
      return;
    }
    if (Notification.permission !== "granted") {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") throw new ApiError("Il permesso per le notifiche non è stato concesso.");
    }
    const token = await runtime.messagingModule.getToken(runtime.messaging, {
      vapidKey: runtime.config.vapidKey,
      serviceWorkerRegistration: runtime.registration,
    });
    if (!token) throw new ApiError("Il browser non ha restituito un token per le notifiche.");
    const response = await api.subscribePush(token);
    pushRuntime.token = token;
    setPushControlState(button, true);
    showLiveMessage(refs.settingsFeedback, response.message || "Notifiche push attivate.");
    await refreshSettingsSilently();
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    setButtonBusy(
      button,
      false,
      button.getAttribute("aria-pressed") === "true"
        ? "Disattiva notifiche push"
        : "Attiva notifiche push",
    );
  }
}

async function logout(button) {
  setButtonBusy(button, true, "Logout…");
  try {
    await api.logout();
    store.set({
      user: null,
      digest: null,
      saved: [],
      history: [],
      visible: { kind: "current", items: [], digestDate: "" },
    });
    activateAssistantContext(null);
    refs.digestList?.replaceChildren();
    renderSavedNews();
    renderHistoryList();
    showAuth("login", "Hai effettuato il logout.");
  } catch (error) {
    showLiveMessage(refs.settingsFeedback, messageFor(error), { error: true });
  } finally {
    closeSettings({ restoreFocus: false });
    setButtonBusy(button, false, "Logout");
  }
}

function initializeSettings() {
  document.querySelectorAll('[data-action="open-settings"]').forEach((button) => {
    button.addEventListener("click", () => void openSettings(button));
  });
  query('[data-action="close-settings"]')?.addEventListener("click", () => closeSettings());
  refs.settingsPage?.addEventListener("click", (event) => {
    if (event.target === refs.settingsPage || event.target.classList.contains("settings-scroll")) closeSettings();
  });
  refs.settingsForm?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-settings-action]");
    if (!button) return;
    const action = button.getAttribute("data-settings-action");
    if (action === "save-email") void saveProfile(button);
    if (action === "save-preferences") void savePreferences(button);
    if (action === "send-newsletter") void sendNewsletter(button);
    if (action === "open-telegram") void openTelegram(button);
    if (action === "toggle-push") void togglePush(button);
    if (action === "edit-suspension") void saveSuspension(button);
    if (action === "cancel-suspension") void clearSuspension(button);
    if (action === "logout") void logout(button);
  });
  const start = query("#settings-pause-start");
  const end = query("#settings-pause-end");
  start?.addEventListener("change", () => { if (end) end.min = start.value || todayIso(); });
}

function initializeAuth() {
  document.querySelectorAll("[data-auth-mode]").forEach((button) => {
    button.addEventListener("click", () => setAuthMode(button.getAttribute("data-auth-mode")));
  });
  document.querySelectorAll('[data-action="toggle-password"]').forEach((button) => {
    button.addEventListener("click", () => {
      const input = button.closest(".auth-control")?.querySelector("input");
      if (!input) return;
      const visible = input.type === "text";
      input.type = visible ? "password" : "text";
      button.setAttribute("aria-label", visible ? "Mostra password" : "Nascondi password");
      button.setAttribute("aria-pressed", String(!visible));
    });
  });
  refs.authForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const state = store.get();
    const mode = state.authMode;
    const email = query('[data-auth-field="email"]')?.value.trim() || "";
    const password = query('[data-auth-field="password"]')?.value || "";
    const submit = query("[data-auth-submit]");
    if (!email || !password) {
      refs.authFeedback.textContent = "Inserisci email e password.";
      return;
    }
    const credentials = {
      email,
      password,
      persistent: Boolean(query('[data-auth-field="remember-me"]')?.checked),
    };
    if (mode === "register") {
      const firstName = query('[data-auth-field="first-name"]')?.value.trim() || "";
      const lastName = query('[data-auth-field="last-name"]')?.value.trim() || "";
      const confirmation = query('[data-auth-field="confirm-password"]')?.value || "";
      if (!firstName || !lastName || password !== confirmation) {
        refs.authFeedback.textContent = password !== confirmation ? "Le password non coincidono." : "Inserisci nome e cognome.";
        return;
      }
      Object.assign(credentials, {
        first_name: firstName,
        last_name: lastName,
        password_confirmation: confirmation,
        email_notifications: Boolean(query('[data-auth-field="email-notifications"]')?.checked),
      });
    }
    setButtonBusy(submit, true, mode === "login" ? "Accesso…" : "Creazione…");
    refs.authFeedback.textContent = "";
    try {
      const response = mode === "login" ? await api.login(credentials) : await api.register(credentials);
      store.set({ user: response.user });
      // La homepage (header con logo, robot, canvas) non va rivelata subito:
      // va prima caricato il contenuto autenticato e ricalcolato il layout
      // (logo/robot dipendono dall'altezza reale del contenitore, nota solo
      // dopo che le card sono nel DOM), altrimenti per un istante l'utente
      // vede il logo nella posizione "vuota" prima che scatti in quella
      // corretta non appena arrivano i dati.
      await loadAuthenticatedData();
      refreshVisualSystem();
      await waitForNextPaint();
      hideAuth();
      refs.authForm.reset();
      query('[data-auth-field="email-notifications"]') && (query('[data-auth-field="email-notifications"]').checked = true);
      if (location.pathname === "/settings") void openSettings();
    } catch (error) {
      refs.authFeedback.textContent = messageFor(error);
    } finally {
      setButtonBusy(submit, false, mode === "login" ? "Accedi" : "Crea account");
    }
  });
}

async function loadAuthenticatedData() {
  if (location.pathname === "/assistant") {
    setAssistantView(true);
    document.title = "Nuvia.AI – Nuvia Assistant";
    refs.digestList?.replaceChildren();
    refs.digestList?.setAttribute("aria-busy", "false");
    if (refs.digestPeriod) refs.digestPeriod.hidden = true;
    hideDigestState();
    setSidebarSection("history", { updateActive: false });
    setSidebarActive("assistant");
    await Promise.allSettled([
      loadSaved(),
      loadHistory({ reset: true }),
    ]);
    return;
  }
  setAssistantView(false);
  const selection = digestSelectionFromLocation();
  await Promise.allSettled([
    selection ? loadHistoricalDigest(selection) : loadCurrentDigest(),
    loadSaved(),
    loadHistory({ reset: true }),
  ]);
}

async function syncLocationState() {
  if (!store.get().user) return;
  if (location.pathname === "/assistant") {
    setAssistantView(true);
    document.title = "Nuvia.AI – Nuvia Assistant";
    refs.digestList?.replaceChildren();
    refs.digestList?.setAttribute("aria-busy", "false");
    if (refs.digestPeriod) refs.digestPeriod.hidden = true;
    hideDigestState();
    setSidebarSection("history", { updateActive: false });
    if (!refs.settingsPage?.classList.contains("hidden")) {
      closeSettings({ restoreFocus: false });
    }
    setSidebarActive("assistant");
    return;
  }
  setAssistantView(false);
  const selection = digestSelectionFromLocation();
  await (selection ? loadHistoricalDigest(selection) : loadCurrentDigest());
  const section = location.pathname === "/saved" ? "saved" : "history";
  setSidebarSection(section, { updateActive: false });
  if (location.pathname === "/settings") {
    void openSettings();
  } else if (!refs.settingsPage?.classList.contains("hidden")) {
    closeSettings({ restoreFocus: false });
    setSidebarActive(location.pathname === "/saved" ? "saved" : "home");
  } else {
    setSidebarActive(location.pathname === "/saved" ? "saved" : "home");
  }
}

async function handleUnauthorized() {
  if (!store.get().user) return;
  store.set({ user: null });
  showAuth("login", "La sessione è scaduta. Accedi di nuovo.");
}

function initializeGlobalEvents() {
  document.addEventListener("keydown", (event) => {
    focusTrap(event);
    if (event.key === "Escape" && !refs.settingsPage?.classList.contains("hidden")) closeSettings();
  });
  query('[data-action="retry-digest"]')?.addEventListener("click", () => {
    const selection = digestSelectionFromLocation();
    void (selection ? loadHistoricalDigest(selection, { focus: true }) : loadCurrentDigest());
  });
  window.addEventListener("offline", () => showAppFeedback("Sei offline. I dati già caricati restano disponibili.", { error: true, timeout: 0 }));
  window.addEventListener("online", () => {
    showAppFeedback("Connessione ripristinata.");
    if (store.get().user) void syncLocationState();
  });
  window.addEventListener("popstate", () => void syncLocationState());
  window.addEventListener("pagehide", () => {
    cancelScheduledVisualRefresh();
    visualSystem?.destroy();
    visualSystem = null;
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) scheduleVisualRefresh({ afterPaint: true });
  });
}

async function start() {
  const brandFontReady = waitForBrandFont();
  try {
    initializeSidebar();
    initializeAssistant();
    initializeSettings();
    initializeAuth();
    initializeGlobalEvents();
    const runtime = await loadRuntimeConfig();
    store.set({ runtime });
    api = new ApiClient(runtime, { onUnauthorized: () => void handleUnauthorized() });
    const response = await api.me();
    store.set({ user: response.user });
    hideAuth();
    await loadAuthenticatedData();
    if (location.pathname === "/settings") void openSettings();
  } catch (error) {
    if (error?.status === 401) {
      showAuth("login");
    } else {
      showAuth("login", `${messageFor(error)} Puoi riprovare ad accedere quando il servizio è disponibile.`);
    }
  } finally {
    await brandFontReady;
    ensureVisualSystem();
    await waitForNextPaint();
    document.body.dataset.appState = "ready";
    scheduleVisualRefresh({ afterPaint: true });
  }
}

void start();

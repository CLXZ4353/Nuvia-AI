export function element(tagName, { className = "", text = null, attributes = {} } = {}) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== null) node.textContent = String(text);
  Object.entries(attributes).forEach(([name, value]) => {
    if (value !== undefined && value !== null) node.setAttribute(name, String(value));
  });
  return node;
}

export function setText(root, selector, value) {
  const node = root.querySelector(selector);
  if (node) node.textContent = String(value || "");
  return node;
}

const NEWS_TITLE_MAX_LENGTH = 80;

function cleanNewsTitle(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^A-Za-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, " ");
}

export function formatNewsTitle(value, fallback = "Senza titolo") {
  const title = cleanNewsTitle(value) || cleanNewsTitle(fallback) || "Notizia";
  if (title.length <= NEWS_TITLE_MAX_LENGTH) {
    return title;
  }

  const boundedTitle = title.slice(0, NEWS_TITLE_MAX_LENGTH).trimEnd();
  const lastSpace = boundedTitle.lastIndexOf(" ");
  return (lastSpace > 0 ? boundedTitle.slice(0, lastSpace) : boundedTitle).trimEnd();
}

export function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""), window.location.origin);
    return ["http:", "https:"].includes(url.protocol) && url.hostname ? url.href : "";
  } catch {
    return "";
  }
}

export function formatSourceLabel(source, fallbackUrl = "") {
  const label = String(source || "").trim();
  if (label && !/^fonte non disponibile$/i.test(label) && !/^(?:https?:)?\/\//i.test(label)) {
    return label.replace(/\s+-\s+/g, " · ");
  }
  try {
    const url = new URL(String(fallbackUrl || ""));
    if (["http:", "https:"].includes(url.protocol) && url.hostname) {
      return url.hostname.replace(/^www\./i, "");
    }
  } catch {
    // The unavailable-source label below keeps the footer understandable.
  }
  return "Fonte non disponibile";
}

export function formatDate(value, fallback = "Data non disponibile") {
  const raw = String(value || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return fallback;
  const date = new Date(`${raw}T12:00:00`);
  if (Number.isNaN(date.valueOf())) return fallback;
  return new Intl.DateTimeFormat("it-IT", { day: "2-digit", month: "long", year: "numeric" }).format(date);
}

export function formatWeekRangeLabel(weekStart, weekEnd, { prefix = "Le novità AI della settimana: ", includeYear = false } = {}) {
  const start = String(weekStart || "").trim();
  const end = String(weekEnd || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end)) return "";
  const startDate = new Date(`${start}T00:00:00Z`);
  const endDate = new Date(`${end}T00:00:00Z`);
  if (Number.isNaN(startDate.valueOf()) || Number.isNaN(endDate.valueOf())) return "";
  const compactDate = (date) => [
    String(date.getUTCDate()).padStart(2, "0"),
    String(date.getUTCMonth() + 1).padStart(2, "0"),
  ].join("/");
  const yearSuffix = includeYear ? ` ${endDate.getUTCFullYear()}` : "";
  return `${prefix}dal ${compactDate(startDate)} al ${compactDate(endDate)}${yearSuffix}`;
}

export function setButtonBusy(button, busy, label = "") {
  if (!button) return;
  button.disabled = busy;
  button.toggleAttribute("aria-busy", busy);
  if (label) {
    const labelNode = button.querySelector("[data-label]") || button.querySelector("span:last-child");
    if (labelNode) labelNode.textContent = label;
  }
}

export function showLiveMessage(node, message, { error = false } = {}) {
  if (!node) return;
  node.textContent = String(message || "");
  node.classList.toggle("is-error", Boolean(error));
  node.classList.toggle("visible", Boolean(message));
}

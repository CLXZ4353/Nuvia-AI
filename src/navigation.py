"""Reusable navigation shell for the local digest web interface.

The GUI is rendered by small server-side HTML functions.  Keeping the sidebar
in this module lets every page share the same visual and interaction contract
without duplicating markup or JavaScript.
"""

from __future__ import annotations

import html


_NAVIGATION_ITEMS = (
    (
        "home",
        "Homepage",
        "&#127968;",
        "/",
        "Vai alla homepage",
        "/",
        "Nuvia.ai – Homepage",
    ),
    (
        "saved",
        "Notizie Salvate",
        "&#128204;",
        "/?sidebar=saved",
        "Apri notizie salvate",
        "/?sidebar=saved",
        "Notizie Salvate",
    ),
    (
        "assistant",
        "Nuvia Assistant",
        "&#128172;",
        "/assistant",
        "Apri Nuvia Assistant",
        "/assistant",
        "Nuvia Assistant",
    ),
    (
        "settings",
        "Impostazioni",
        "&#9881;",
        "/settings",
        "Impostazioni",
        "/settings?sidebar=open",
        "Impostazioni",
    ),
    (
        "history",
        "Cronologia",
        "&#128343;",
        "/?sidebar=open",
        "Apri cronologia",
        "/?sidebar=open",
        "Cronologia",
    ),
)


def render_sidebar_css() -> str:
    """Return the shared sidebar styles.

    The closed state deliberately contains only the three requested navigation
    icons.  Labels slide in only after the sidebar receives ``sidebar-open`` on
    the page body.
    """

    return """
    .app-sidebar {
      position: fixed;
      inset: 0 auto 0 0;
      z-index: 12;
      width: 58px;
      overflow: hidden;
      border-right: 1px solid var(--line);
      background: var(--paper);
      box-shadow: 5px 0 18px rgba(23, 32, 42, 0.08);
      transition: width 180ms ease, box-shadow 180ms ease;
    }
    body.sidebar-open .app-sidebar {
      width: min(270px, calc(100vw - 32px));
      overflow-x: hidden;
      overflow-y: auto;
      box-shadow: 10px 0 28px rgba(23, 32, 42, 0.16);
    }
    .app-sidebar-nav {
      display: grid;
      align-content: start;
      padding: 14px 0;
    }
    .app-sidebar-link {
      display: grid;
      grid-template-columns: 58px max-content;
      align-items: center;
      width: 100%;
      min-width: 0;
      min-height: 46px;
      padding: 6px 0;
      border: 0;
      border-left: 3px solid transparent;
      color: var(--ink);
      text-decoration: none;
      transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
    }
    .app-sidebar-link:hover,
    .app-sidebar-link:focus-visible {
      background: var(--accent-soft);
      color: var(--accent);
      outline: none;
    }
    .app-sidebar-link.active {
      border-left-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
    }
    .app-sidebar-icon {
      display: inline-grid;
      width: 55px;
      place-items: center;
      font-size: 20px;
      line-height: 1;
    }
    .app-sidebar-label {
      overflow: hidden;
      opacity: 0;
      font-size: 15px;
      font-weight: 700;
      white-space: nowrap;
      transform: translateX(-8px);
      transition: opacity 140ms ease 30ms, transform 180ms ease;
    }
    body.sidebar-open .app-sidebar-label {
      opacity: 1;
      transform: translateX(0);
    }
    .history-panel,
    .saved-news-panel {
      display: none;
      min-width: 0;
      padding: 0 12px 16px;
    }
    body.sidebar-open .history-panel,
    body.sidebar-open .saved-news-panel {
      display: grid;
      gap: 10px;
    }
    .history-panel .history-search {
      width: 100%;
      min-width: 0;
      margin: 0;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--paper);
      color: var(--ink);
      font: inherit;
    }
    .history-panel .history-search:focus {
      border-color: var(--accent);
      outline: 2px solid var(--accent-soft);
    }
    .history-search-status {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }
    .history-list {
      display: grid;
      align-content: start;
      grid-auto-rows: max-content;
      gap: 8px;
      max-height: calc(100vh - 292px);
      overflow-y: auto;
      overscroll-behavior: contain;
      padding-right: 2px;
    }
    .saved-news-list {
      display: grid;
      align-content: start;
      gap: 8px;
      max-height: calc(100vh - 100px);
      overflow-y: auto;
      overscroll-behavior: contain;
      padding-right: 2px;
    }
    .saved-news-item {
      display: grid;
      gap: 3px;
      padding: 10px 11px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--paper);
      color: var(--ink);
      text-decoration: none;
    }
    .saved-news-item:hover,
    .saved-news-item:focus-visible {
      border-color: var(--accent);
      background: var(--accent-soft);
      outline: none;
    }
    .saved-news-title {
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
    }
    .saved-news-date {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .saved-news-empty {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .history-item {
      min-height: max-content;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--paper);
    }
    .history-item[hidden] { display: none; }
    .history-summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-height: 48px;
      padding: 10px 11px;
      cursor: pointer;
      list-style: none;
    }
    .history-summary::-webkit-details-marker { display: none; }
    .history-summary:hover,
    .history-summary:focus-visible {
      background: var(--accent-soft);
      outline: none;
    }
    .history-title,
    .history-meta {
      display: block;
    }
    .history-title {
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
    }
    .history-meta {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .history-arrow {
      flex: 0 0 auto;
      color: var(--accent);
      transition: transform 150ms ease;
    }
    .history-item[open] .history-arrow { transform: rotate(180deg); }
    .history-body {
      border-top: 1px solid var(--line);
      padding: 10px 11px 11px;
    }
    .history-source-list {
      display: grid;
      gap: 7px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .history-source-card {
      display: grid;
      gap: 4px;
      padding: 8px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--bg);
    }
    .history-source-title,
    .history-source-link {
      display: block;
    }
    .history-source-title {
      font-size: 13px;
      font-weight: 700;
      line-height: 1.4;
    }
    .history-source-link {
      overflow-wrap: anywhere;
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      text-decoration: none;
    }
    .history-source-card:focus-within { border-color: var(--accent); }
    .history-source-link:hover,
    .history-source-link:focus-visible { text-decoration: underline; }
    .read-more {
      display: inline-block;
      margin-top: 10px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
    }
    .read-more:hover,
    .read-more:focus-visible { text-decoration: underline; }
    .history-empty {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    mark.search-highlight {
      padding: 0 1px;
      border-radius: 2px;
      background: #ffe49b;
      color: inherit;
    }
    .app-sidebar-scrim {
      position: fixed;
      inset: 0;
      z-index: 11;
      background: transparent;
      pointer-events: none;
      transition: background 180ms ease;
    }
    body.sidebar-open .app-sidebar-scrim {
      background: var(--shade, rgba(23, 32, 42, 0.35));
      pointer-events: auto;
    }
    @media (max-width: 640px) {
      .app-sidebar { width: 54px; }
      .app-sidebar-link { grid-template-columns: 54px max-content; }
      .app-sidebar-icon { width: 51px; }
    }
    """


def render_sidebar(
    active_section: str | None = None,
    history_content: str = "",
    saved_content: str = "",
) -> str:
    """Render the three-item application sidebar and its light interaction JS."""

    links: list[str] = []
    for section, label, icon, href, aria_label, sidebar_target, tooltip in _NAVIGATION_ITEMS:
        active = section == active_section
        active_class = " active" if active else ""
        current = ' aria-current="page"' if active else ""
        links.append(
            f'<a class="app-sidebar-link{active_class}" href="{html.escape(href, quote=True)}" '
            f'data-sidebar-nav data-sidebar-target="{html.escape(sidebar_target, quote=True)}" '
            f'aria-label="{html.escape(aria_label)}" title="{html.escape(tooltip)}"{current}>'
            f'<span class="app-sidebar-icon" aria-hidden="true">{icon}</span>'
            f'<span class="app-sidebar-label">{html.escape(label)}</span>'
            "</a>"
        )
        if section == "saved" and saved_content:
            links.append(saved_content)
        if section == "history" and history_content:
            links.append(history_content)
    return f"""
  <aside class="app-sidebar" aria-label="Navigazione principale">
    <nav class="app-sidebar-nav">{''.join(links)}</nav>
  </aside>
  <div class="app-sidebar-scrim" data-sidebar-scrim></div>
  <script>
  (() => {{
    const body = document.body;
    const close = () => body.classList.remove("sidebar-open");
    document.querySelector("[data-sidebar-scrim]")?.addEventListener("click", close);
    document.addEventListener("keydown", (event) => {{
      if (event.key === "Escape") close();
    }});
    document.querySelectorAll("[data-sidebar-nav]").forEach((link) => {{
      link.addEventListener("click", (event) => {{
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        body.classList.add("sidebar-open");
        const target = link.dataset.sidebarTarget || link.href;
        window.setTimeout(() => window.location.assign(target), 160);
      }});
    }});
  }})();
  </script>"""

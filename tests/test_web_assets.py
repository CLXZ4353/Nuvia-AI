"""Regression checks for the production static frontend shell."""

from __future__ import annotations

from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1] / "src" / "web"


def test_web_shell_references_real_local_assets_and_dynamic_mounts():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert '<meta name="viewport"' in page
    assert 'body[data-app-state="booting"] { opacity: 0; }' in page
    assert page.index('body[data-app-state="booting"] { opacity: 0; }') < page.index('href="/assets/css/styles.css?v=20260722-login-chat-layout"')
    assert 'href="/assets/css/styles.css?v=20260722-login-chat-layout"' in page
    assert 'src="/assets/images/robot.gif"' in page
    assert 'class="brand-header"' in page
    assert 'class="brand-lockup" role="img" aria-label="Nuvia.ai"' in page
    assert '<span class="brand-initial">N</span>' in page
    assert '<span class="brand-tail">uvia.ai</span>' in page
    assert 'class="brand-swoosh"' in page
    assert 'src="/assets/images/nuvia-logo.png"' not in page
    assert 'src="/assets/js/app.js?v=20260722-login-chat-layout"' in page
    assert 'data-ui="digest-list"' in page
    assert 'data-ui="digest-period"' in page
    assert 'data-ui="saved-list"' in page
    assert 'data-ui="history-list"' in page
    assert (WEB_ROOT / "assets" / "images" / "robot.gif").is_file()
    assert (WEB_ROOT / "assets" / "fonts" / "alex-brush-regular.ttf").is_file()
    assert (WEB_ROOT / "assets" / "fonts" / "alex-brush-OFL.txt").is_file()
    assert (WEB_ROOT / "assets" / "fonts" / "inter-latin-variable.woff2").is_file()


def test_assistant_page_mounts_the_reference_chat_only_on_its_route():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")
    sidebar = page[page.index('<aside id="sidebar"') : page.index("</aside>")]
    chat_css_start = css.index(".assistant-chat-window {")
    chat_css = css[chat_css_start : css.index("}", chat_css_start)]

    assert 'id="assistant-page"' in page
    assert 'class="assistant-chat-window"' in page
    assert "Ciao sono nuvia!<br>quale notizia vorresti approfondire?" in page
    assert 'placeholder="Write a message..."' in page
    assert 'class="assistant-chat-tail"' not in page
    assert page.index('class="brand-header"') < page.index('id="assistant-page"') < page.index('id="cards-overlay"')
    assert 'class="sidebar-nav-item sidebar-assistant-link"' in sidebar
    assert sidebar.index('aria-label="Cronologia"') < sidebar.index('aria-label="Apri Nuvia Assistant"')

    assert "function setAssistantView(active)" in app
    assert 'document.body.classList.toggle("assistant-view", visible);' in app
    assert 'refs.assistantPage.hidden = !visible;' in app
    assert 'const assistantActive = location.pathname === "/assistant";' in app
    assert '"(max-width: 1200px), (max-height: 760px)",' in app
    assert "const assistantDesktop = assistantActive && !window.matchMedia(" in app
    assert "if (assistantDesktop) refs.sidebar?.classList.remove(\"closed\");" in app
    assistant_loader = app[app.index("async function loadAuthenticatedData()") : app.index("async function syncLocationState()")]
    assert "loadSaved()," in assistant_loader
    assert "loadHistory({ reset: true })," in assistant_loader
    assert 'setSidebarSection("history", { updateActive: false });' in assistant_loader
    assert 'setSidebarActive("assistant");' in assistant_loader

    assert ".assistant-chat-window {" in css
    assert "left: 53.25%;" in chat_css
    assert "width: clamp(504px, 33vw, 660px);" in css
    assert "height: clamp(620px, 61vh, 782px);" in css
    assert "backdrop-filter: blur(20px) saturate(108%);" in css
    assert ".assistant-chat-tail" not in css
    assert "body.auth-open .assistant-page" in css


def test_assistant_chat_is_measured_by_the_neural_and_robot_layout():
    animation = (WEB_ROOT / "assets" / "js" / "animation.js").read_text(encoding="utf-8")

    assert 'const assistantChat = documentRef.querySelector(".assistant-chat-window");' in animation
    assert "function activeAssistantSurface()" in animation
    assert 'documentRef.body?.classList.contains("assistant-view")' in animation
    assert "return activeAssistantSurface() || overlay;" in animation
    assert "const overlayRect = layoutSurface().getBoundingClientRect();" in animation
    assert "return [assistantSurface];" in animation
    assert "if (assistantChat) resizeObserver.observe(assistantChat);" in animation
    assert "assistantRobotWidthRatio: 0.5835," in animation
    assert "assistantRobotMaxWidth: 1167," in animation
    assert "assistantVisibleLeftInsetRatio: 0.375," in animation
    assert "assistantVisibleTopWithinChatRatio: 0.468," in animation
    assert "chatRect.right" in animation
    assert "chatRect.top" in animation
    assert "if (robotMetrics.assistant) {" in animation


def test_brand_logo_is_centered_responsive_and_kept_above_the_cards():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")

    assert page.index('class="brand-header"') < page.index('id="cards-overlay"')
    assert page.index('class="brand-header"') < page.index('data-ui="digest-period"') < page.index('id="cards-overlay"')
    assert ".brand-header {" in css
    assert "justify-content: center;" in css
    assert "font-family: 'Alex Brush', cursive;" in css
    assert "width: min(780px, 82vw);" in css
    assert "font-size: clamp(132px, 16vw, 225px);" in css
    assert "font-size: clamp(88px, 12.8vw, 180px);" in css
    assert "margin-right: 0;" in css
    assert "transform: translateY(clamp(-54px, -3.7vw, -24px)) scale(0.7);" in css
    assert ".brand-swoosh-stroke {" in css
    assert "transform: translateY(-0.7cm);" in css
    assert "transform: translateY(-0.5cm);" in css
    assert 'body[data-app-state="booting"] .brand-header' in css
    assert 'body[data-app-state="booting"] #cards-overlay' in css
    assert "body.settings-open .brand-header" in css
    assert "body.settings-open #robot-container" in css
    assert "body.auth-open .brand-header" in css


def test_digest_period_uses_the_visible_digest_collection_week_and_card_title_typography():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")
    dom = (WEB_ROOT / "assets" / "js" / "dom.js").read_text(encoding="utf-8")

    assert '<p id="digestPeriod" class="digest-period"' in page
    assert ".digest-period {" in css
    assert "color: var(--label-color);" in css
    assert "font-weight: 700;" in css
    assert "line-height: var(--type-display-line);" in css
    assert "text-align: center;" in css
    assert "export function formatWeekRangeLabel(weekStart, weekEnd" in dom
    assert "Le novità AI della settimana: " in dom
    assert "dal ${compactDate(startDate)} al ${compactDate(endDate)}" in dom
    assert "renderDigestPeriod(visible.weekStart, visible.weekEnd);" in app
    assert "weekStart: digest.week_start || \"\"" in app
    assert "weekEnd: digest.week_end || \"\"" in app
    assert "body.settings-open .digest-period" in css
    assert "body.auth-open .digest-period" in css


def test_frontend_modules_do_not_restore_demo_persistence_or_unsafe_rendering():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((WEB_ROOT / "assets" / "js").glob("*.js"))
    )

    for forbidden in ("localStorage", "innerHTML", "document.write", "localhost", "127.0.0.1"):
        assert forbidden not in source
    assert 'const ASSISTANT_CONTEXT_STORAGE_KEY = "nuvia.assistant.active-news.v1";' in source


def test_existing_assistant_chat_is_functional_and_cards_open_it_with_news_context():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")
    api = (WEB_ROOT / "assets" / "js" / "api.js").read_text(encoding="utf-8")
    renderer = (WEB_ROOT / "assets" / "js" / "render.js").read_text(encoding="utf-8")

    template = page[page.index('<template id="digest-card-template">') :]
    assert template.index('data-field="why"') < template.index('data-action="open-assistant"')
    assert "Approfondisci la notizia" in template
    assert 'data-ui="assistant-context"' in page
    assert 'data-ui="assistant-message-list"' in page
    assert 'data-ui="assistant-input"' in page
    assert 'maxlength="4000"' in page
    assert 'data-ui="assistant-send"' in page
    assert 'data-ui="assistant-status"' in page

    assert "onOpenAssistant?.(item);" in renderer
    assert "onOpenAssistant: openAssistantForNews" in app
    assert "function openAssistantForNews(item)" in app
    assert "Sintesi completa disponibile:" in app
    assert "Perché conta:" in app
    assert 'window.history.pushState({}, "", "/assistant")' in app
    assert 'const ASSISTANT_CONTEXT_PROMPT = "Che cosa vorresti approfondire di questa notizia?";' in app
    assert "assistantHistory = [];" in app
    assert "assistantHistory.slice(-12)" in app
    assert "assistantPreviousContext" in app
    assert "ASSISTANT_COMPARISON_PATTERN.test(message)" in app
    assert "refs.assistantComposer?.requestSubmit();" in app
    assert "Nuvia sta elaborando la risposta…" in app
    assert "Invio non riuscito. La barra è pronta per riprovare." in app

    assert 'return this.request("/assistant"' in api
    assert "comparison_context: comparisonContext" in api
    assert "timeoutMs: Math.max(this.timeoutMs, 75_000)" in api
    assert ".assistant-message-list {" in css
    assert "overflow-y: auto;" in css
    assert ".assistant-composer-button:disabled" in css
    assert ".info-card .card-deepen-button" in css


def test_assistant_messages_autosize_scroll_and_render_markdown_safely():
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")

    assert "function appendAssistantInlineMarkdown(parent, value)" in app
    assert "function renderAssistantMarkdown(container, value)" in app
    assert 'element("strong")' in app
    assert 'element("em")' in app
    assert 'element(nextListType, { className: "assistant-markdown-list" })' in app
    assert 'className: "assistant-markdown-link"' in app
    assert "renderAssistantMarkdown(contentNode, content);" in app
    assert "innerHTML" not in app
    assert 'behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"' in app

    assert "scroll-behavior: smooth;" in css
    assert "scrollbar-gutter: stable;" in css
    assert "display: block;" in css
    assert "flex: none;" in css
    assert "height: auto;" in css
    assert "max-width: min(88%, 440px);" in css
    assert ".assistant-message-content p + p" in css
    assert ".assistant-markdown-heading" in css
    assert ".assistant-markdown-list li + li" in css
    assert ".assistant-markdown-link" in css


def test_history_previews_are_plain_text_and_read_more_opens_the_date_digest():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    renderer = (WEB_ROOT / "assets" / "js" / "render.js").read_text(encoding="utf-8")
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")

    assert '<span class="accordion-news-title" data-field="title"></span>' in page
    assert '<a class="accordion-news-title"' not in page
    assert '<a class="accordion-read-more" data-action="open-digest">Leggi di più</a>' in page
    assert 'renderHistory(refs.historyList, refs.historyState, state.history, { digestHref: digestLocationFor })' in app
    assert 'const href = digestHref?.(digest);' in renderer
    assert 'if (readMore && href) readMore.href = href;' in renderer
    assert 'focus: visible.kind === "saved"' in app


def test_card_footer_uses_a_readable_source_label_without_publication_date():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    renderer = (WEB_ROOT / "assets" / "js" / "render.js").read_text(encoding="utf-8")
    dom = (WEB_ROOT / "assets" / "js" / "dom.js").read_text(encoding="utf-8")

    assert '<span class="footer-label">Link sito:</span>' in page
    assert "Pubblicata:" not in page
    assert "publishedAtWrapper" not in page
    assert "publishedAt" not in renderer
    assert 'formatSourceLabel(item.source, sourceUrl)' in renderer
    assert 'setText(card, \'[data-field="linkLabel"]\', sourceUrl' not in renderer
    assert 'return label.replace(/\\s+-\\s+/g, " · ");' in dom
    assert 'return "Fonte non disponibile";' in dom


def test_card_titles_are_limited_to_plain_eighty_character_text():
    renderer = (WEB_ROOT / "assets" / "js" / "render.js").read_text(encoding="utf-8")
    dom = (WEB_ROOT / "assets" / "js" / "dom.js").read_text(encoding="utf-8")

    assert "const NEWS_TITLE_MAX_LENGTH = 80;" in dom
    assert "export function formatNewsTitle" in dom
    assert ".replace(/[^A-Za-z0-9]+/g, \" \")" in dom
    assert "formatNewsTitle(item.title)" in renderer
    assert 'formatNewsTitle(item.title || item.source, "Notizia")' in renderer


def test_card_typography_uses_the_requested_sizes_on_every_viewport():
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")

    assert "--type-display-size: 24px;" in css
    assert "--type-label-size: 15px;" in css
    assert "--type-body-size: 14px;" in css
    assert ".info-card .card-meta .meta-content {\n  font-family: var(--font-sans);\n  font-size: 14px;" in css
    assert ".info-card .footer-link {\n  display: block;\n  font-size: 15px;" in css


def test_digest_pages_load_cards_without_a_transient_loading_bar():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")
    historical_start = app.index("async function loadHistoricalDigest")
    historical_end = app.index("function openSavedNews", historical_start)
    historical_loader = app[historical_start:historical_end]
    current_start = app.index("async function loadCurrentDigest")
    current_end = app.index("async function loadSaved", current_start)
    current_loader = app[current_start:current_end]

    assert 'id="digestState"' in page
    assert 'aria-live="polite" hidden>' in page
    assert 'class="content-state content-state--loading"' not in page
    assert 'data-ui="digest-message"></p>' in page
    assert "Apertura del digest storico" not in app
    assert "Caricamento del digest" not in app
    assert "function hideDigestState()" in app
    assert "hideDigestState();" in historical_loader
    assert "hideDigestState();" in current_loader
    assert "renderDigestState(refs.digestState, { kind: \"loading\"" not in historical_loader
    assert 'kind: "history",' in historical_loader
    assert 'digestDate: digest.digest_date || "",' in historical_loader
    assert "renderVisibleDigest();" in historical_loader
    assert "renderDigestState(refs.digestState, { kind: \"loading\"" not in current_loader


def test_bootstrap_and_network_refresh_wait_for_the_first_card_paint():
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")
    config = (WEB_ROOT / "assets" / "js" / "config.js").read_text(encoding="utf-8")
    animation = (WEB_ROOT / "assets" / "js" / "animation.js").read_text(encoding="utf-8")
    start = app[app.index("async function start()") :]

    assert "function ensureVisualSystem()" in app
    assert "async function waitForBrandFont()" in app
    assert 'document.fonts.load(BRAND_FONT_DESCRIPTOR, "Nuvia.ai")' in app
    assert "function waitForNextPaint()" in app
    assert "function scheduleVisualRefresh({ afterPaint = false } = {})" in app
    assert app.count("scheduleVisualRefresh({ afterPaint: true });") >= 3
    assert "visualSystem = createVisualSystem" not in start
    assert "const brandFontReady = waitForBrandFont();" in start
    assert "await brandFontReady;" in start
    assert start.index("ensureVisualSystem();") < start.index('document.body.dataset.appState = "ready";')
    assert start.index("await waitForNextPaint();") < start.index('document.body.dataset.appState = "ready";')
    assert "const controller = new AbortController();" in config
    assert "CONFIG_REQUEST_TIMEOUT_MS = 4_000" in config
    assert "signal: controller.signal" in config
    assert "let postPaintRefreshFrame = 0;" in animation
    assert "postPaintRefreshFrame = windowRef.requestAnimationFrame" in animation


def test_robot_is_revealed_only_after_its_initial_position_is_calculated():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    animation = (WEB_ROOT / "assets" / "js" / "animation.js").read_text(encoding="utf-8")

    assert '<div id="robot-container" class="is-positioning" aria-hidden="true">' in page
    assert "#robot-container.is-positioning { visibility: hidden; }" in css
    assert "function revealRobotAtInitialPosition()" in animation
    assert "const transform = `translate3d(0, ${translateY.toFixed(2)}px, 0)`;" in animation
    assert "robot.style.transform = transform;" in animation
    assert 'robot.style.transition = "none";' in animation
    assert 'robot.classList.remove("is-positioning");' in animation


def test_robot_scroll_uses_a_compositor_only_path_and_pauses_network_work():
    animation = (WEB_ROOT / "assets" / "js" / "animation.js").read_text(encoding="utf-8")
    layout_start = animation.index("function applyRobotLayout()")
    layout_end = animation.index("function animateRobotPosition", layout_start)
    layout = animation[layout_start:layout_end]
    scroll_start = animation.index("function scheduleRobotPosition()")
    scroll_end = animation.index("function hasNetworkLayoutChanged", scroll_start)
    scroll = animation[scroll_start:scroll_end]

    assert "function pauseNetworkForScroll()" in animation
    assert "networkPausedForScroll = true;" in animation
    assert "NETWORK.scrollIdleDelay" in animation
    assert 'windowRef.addEventListener("scroll", onWindowScroll, { passive: true });' in animation
    assert "robot.style.position" in layout
    assert "robot.style.top" in layout
    assert "robot.style.left" in layout
    assert "robot.style.position" not in scroll
    assert "robot.style.top" not in scroll
    assert "robot.style.left" not in scroll
    assert "setRobotTransform(robotRenderedTranslateY);" in animation


def test_mobile_robot_is_anchored_to_the_first_card_without_losing_animation():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    animation = (WEB_ROOT / "assets" / "js" / "animation.js").read_text(encoding="utf-8")
    mobile_start = css.index("@media (max-width: 1200px), (max-height: 760px)")
    mobile_end = css.index("@media (max-width: 420px)", mobile_start)
    mobile = css[mobile_start:mobile_end]
    phone_start = css.index("@media (max-width: 520px)")
    phone_end = css.index("@media (max-width: 380px)", phone_start)
    phone = css[phone_start:phone_end]
    scroll_start = animation.index("function onWindowScroll()")
    scroll_end = animation.index('windowRef.addEventListener("resize"', scroll_start)
    scroll = animation[scroll_start:scroll_end]

    assert 'src="/assets/images/robot.gif"' in page
    assert "padding-top: clamp(34px, 4.5vh, 48px);" in mobile
    assert "transform: translateY(-14px) scale(0.7);" in mobile
    assert ".brand-wordmark {\n    top: -10px;" in mobile
    assert "top: clamp(105px, 15.5vw, 160px);" in mobile
    assert "bottom: auto;" in mobile
    assert "height: 18%;" in mobile
    assert "transform: none;" in mobile
    assert "#robot-container {" in mobile
    assert "position: absolute;" in mobile
    assert "width: clamp(280px, 96vw, 460px);" in mobile
    assert "z-index: 4;" in mobile
    assert "left: clamp(10px, 2vw, 16px);" in mobile
    assert "position: relative;\n  z-index: 5;" in css
    assert "if (!isMobile()) {" in scroll
    assert "scheduleRobotPosition();" in scroll
    assert "mobileBreakpoint: 1200," in animation
    assert "compactHeightBreakpoint: 760," in animation
    assert "phoneCardOverlapRatio: 0.445," in animation
    assert "tabletCardOverlapRatio: 0.52," in animation
    assert "(max-height: ${ROBOT.compactHeightBreakpoint}px)" in animation
    assert "firstCard.getBoundingClientRect().top - containerRect.top" in animation
    assert 'const robotImage = robot.querySelector("img");' in animation
    assert "const robotWidth = robotRect.width || robot.offsetWidth || 0;" in animation
    assert "const robotHeight = robotRect.height || robotWidth * (9 / 16);" in animation
    assert "const cardOverlapRatio = isPhone()" in animation
    assert "const cardAnchoredTop = firstCardTop - robotHeight * cardOverlapRatio;" in animation
    assert "baseTop: Math.max(0, cardAnchoredTop)," in animation
    mobile_metrics = animation[
        animation.index("function refreshRobotMetrics()") : animation.index(
            "const viewportWidth", animation.index("function refreshRobotMetrics()")
        )
    ]
    assert 'documentRef.querySelector(".brand-header")' not in mobile_metrics
    mobile_layout = animation[
        animation.index("if (isMobile()) {", animation.index("function applyRobotLayout()")) :
        animation.index("if (robotMetrics.assistant)", animation.index("function applyRobotLayout()"))
    ]
    assert 'robot.style.position = "absolute";' in mobile_layout
    assert 'robot.style.position = "fixed";' not in mobile_layout
    assert 'robot.style.transform = "translate3d(-50%, 0, 0)";' in animation
    assert "addMediaListener(phoneMedia, scheduleRefresh);" in animation
    assert "removeMediaListener(phoneMedia, scheduleRefresh);" in animation
    assert 'robotImage?.addEventListener("load", scheduleRefresh);' in animation
    assert 'robotImage?.removeEventListener("load", scheduleRefresh);' in animation
    assert "height: 74px;" in phone
    assert "margin-bottom: calc(3cm + 0.5cm);" in phone


def test_portrait_mobile_sidebar_gets_only_a_subtle_white_glass_boost():
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    media = "@media (max-width: 900px) and (orientation: portrait) {"
    start = css.index(media)
    end = css.index("@media (max-width: 520px)", start)
    portrait = css[start:end]
    sidebar_start = portrait.index(".sidebar {")
    sidebar_end = portrait.index("\n  }", sidebar_start)
    sidebar = portrait[sidebar_start:sidebar_end]

    assert ".sidebar {" in sidebar
    assert "rgba(255, 255, 255, 0.23) 0%" in sidebar
    assert "rgba(255, 255, 255, 0.15) 45%" in sidebar
    assert "rgba(255, 255, 255, 0.11) 100%" in sidebar
    assert "background-color: rgba(116, 139, 179, 0.16);" in sidebar
    assert "backdrop-filter: blur(9px) saturate(105%);" in sidebar
    assert "-webkit-backdrop-filter: blur(9px) saturate(105%);" in sidebar
    assert "width:" not in sidebar
    assert "height:" not in sidebar
    assert "position:" not in sidebar


def test_portrait_smartphone_cards_move_down_without_affecting_other_layouts():
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    media = "@media (max-width: 520px) and (orientation: portrait) {"
    start = css.index(media)
    end = css.index("@media (max-width: 380px)", start)
    portrait_phone = css[start:end]

    assert "#cards-overlay {" in portrait_phone
    assert "transform: translateY(3cm);" in portrait_phone
    assert ".brand-header" not in portrait_phone
    assert "#robot-container" not in portrait_phone
    assert ".sidebar" not in portrait_phone


def test_robot_has_no_extra_glow_filter():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")

    assert "robot-alpha-cleanup" not in page
    assert "robot-alpha-cleanup" not in css
    assert ".visual-filter-defs" not in css


def test_sidebar_keeps_homepage_only_in_expanded_navigation():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    collapsed_start = page.index('<div class="sidebar-collapsed-only">')
    collapsed_end = page.index('<div class="sidebar-expanded-only">', collapsed_start)
    collapsed = page[collapsed_start:collapsed_end]
    nav_start = page.index('<nav class="sidebar-nav-block"')
    nav_end = page.index("</nav>", nav_start)
    nav = page[nav_start:nav_end]
    homepage_start = nav.index('href="/"')
    saved_start = nav.index('aria-label="Notizie salvate"')

    assert "sidebar-home-icon-collapsed" not in collapsed
    assert 'aria-label="Homepage"' not in collapsed
    assert '<a href="/" class="sidebar-nav-item sidebar-home-link" aria-label="Homepage" data-sidebar-key="home">' in nav
    assert homepage_start < saved_start < nav.index('aria-label="Impostazioni"') < nav.index('aria-label="Cronologia"') < nav.index('aria-label="Apri Nuvia Assistant"')
    assert "data-sidebar-section" not in nav[homepage_start:saved_start]
    assert "--sidebar-closed-height: 257px;" in css
    assert "color: inherit;" in css
    assert "text-decoration: none;" in css


def test_sidebar_expanded_content_stays_in_layout_and_reveals_after_shell_expands():
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    timing = "0.5s cubic-bezier(0.65, 0, 0.35, 1)"

    assert ".sidebar.transitions-enabled {" in css
    assert ".sidebar.transitions-enabled .sidebar-expanded-only {" in css
    assert ".sidebar.closed .sidebar-expanded-only {" in css
    sidebar_transition_start = css.index(".sidebar.transitions-enabled {")
    sidebar_transition_end = css.index("}", sidebar_transition_start)
    sidebar_transition = css[sidebar_transition_start:sidebar_transition_end]
    expanded_transition_start = css.index(".sidebar.transitions-enabled .sidebar-expanded-only {")
    expanded_transition_end = css.index("}", expanded_transition_start)
    expanded_transition = css[expanded_transition_start:expanded_transition_end]
    closed_expanded_start = css.index(".sidebar.closed .sidebar-expanded-only {")
    closed_expanded_end = css.index("}", closed_expanded_start)
    closed_expanded = css[closed_expanded_start:closed_expanded_end]

    assert f"width {timing}" in sidebar_transition
    assert "display: none;" not in closed_expanded
    assert "display: flex;" in closed_expanded
    assert "opacity: 0;" in closed_expanded
    assert "visibility: hidden;" in closed_expanded
    assert "pointer-events: none;" in closed_expanded
    assert "opacity 160ms cubic-bezier(0.22, 1, 0.36, 1) 340ms" in expanded_transition
    assert "visibility 0s linear 340ms" in expanded_transition
    assert "transform" not in expanded_transition
    assert "transform:" not in closed_expanded
    assert ".sidebar.transitions-enabled .sidebar-collapsed-only {" in css
    assert "position: absolute;" in css
    assert "grid-template-columns: 40px minmax(0, 1fr);" in css
    assert "white-space: nowrap;" in css


def test_sidebar_has_one_explicit_active_section_without_hover_selection():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "assets" / "css" / "styles.css").read_text(encoding="utf-8")
    app = (WEB_ROOT / "assets" / "js" / "app.js").read_text(encoding="utf-8")

    for section in ("home", "saved", "settings", "history", "assistant"):
        assert f'data-sidebar-key="{section}"' in page
    assert "function setSidebarActive(section)" in app
    assert 'node.classList.toggle("is-active", active);' in app
    assert 'setSidebarActive("settings");' in app
    hover_start = css.index(".sidebar-nav-item:hover {")
    hover = css[hover_start : css.index("}", hover_start)]
    assert "background: transparent;" in hover


def test_neural_network_uses_the_reference_web_structure_and_render_order():
    animation = (WEB_ROOT / "assets" / "js" / "animation.js").read_text(encoding="utf-8")

    for value in (
        "particleCount: 380",
        "edgeParticles: 130",
        "topSideHeight: 260",
        "topSideEdgeKeepRatio: 0.78",
        "maxDistance: 170",
        "maxConnectionsPerParticle: 6",
        "hubChance: 0.18",
        "hubMaxConnections: 10",
        "lineOpacityMultiplier: 0.65",
        "lineWidth: 0.6",
        "particleSpeed: 0.22",
        "particleRadius: 1.3",
        "maxGapRatio: 0.44",
        "cardsBottomMargin: 170",
        "gapFadeDistance: 110",
        "edgeBiasPower: 3.2",
        "bottomFillSpacing: 90",
    ):
        assert value in animation

    assert "lineColor: [255, 255, 255]" in animation
    assert "canvasWidth = canvas.width = canvas.offsetWidth;" in animation
    assert "canvasHeight = canvas.height = canvas.offsetHeight;" in animation
    assert "function scaledParticleCounts()" in animation
    assert "function addBottomFillParticles()" in animation
    assert "function shouldKeepTopSideEdgeParticle(particle)" in animation
    assert "class Particle" in animation
    assert "function buildConnectionGrid()" in animation
    assert "function connectParticles()" in animation
    assert "opacity.toFixed(2)" in animation
    assert "particle.update();\n      particle.draw();\n    });\n    connectParticles();" in animation

    for removed_visual_limit in (
        "particleBudget",
        "maxDevicePixelRatio",
        "maxCanvasPixels",
    ):
        assert removed_visual_limit not in animation

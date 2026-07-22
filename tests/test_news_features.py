import http.client
import json
import threading
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from src import auth, gui


@pytest.fixture
def news_record():
    return {
        "id": "n_1234567890abcdef12345678",
        "title": "Nuovo modello per la ricerca AI",
        "meta": "Fonte: Laboratorio Test - Data: 2026-07-14",
        "url": "https://example.com/research",
        "summary": "Il contenuto completo della ricerca è disponibile qui.",
        "why": "Aiuta a valutare i nuovi strumenti.",
        "digest_title": "AI Research Digest - 14/07/2026",
        "digest_date": "2026-07-14",
    }


@pytest.fixture
def news_store(tmp_path, monkeypatch, news_record):
    monkeypatch.setattr(auth, "AUTH_STORE_PATH", tmp_path / "auth_store.json")
    monkeypatch.setattr(gui, "SAVED_NEWS_PATH", tmp_path / "saved_news.json")
    monkeypatch.setattr(gui, "_all_news_records", lambda: [news_record])
    return news_record


def _register(email="ada@example.com"):
    result = auth.register_user("Ada", "Lovelace", email, "SecurePass1!", "SecurePass1!")
    assert result.ok
    return result.user


def test_saved_news_is_persistent_and_account_scoped(news_store):
    first_user = _register()
    second_user = _register("grace@example.com")

    assert gui.save_news_for_user(first_user, news_store)
    assert not gui.save_news_for_user(first_user, news_store)
    assert gui.saved_news_ids_for_user(first_user) == {news_store["id"]}
    assert gui.saved_news_for_user(second_user) == []

    payload = json.loads(gui.SAVED_NEWS_PATH.read_text(encoding="utf-8"))
    assert list(payload["users"]) == [first_user["id"]]
    assert payload["users"][first_user["id"]][0]["title"] == news_store["title"]


def test_saved_news_sidebar_lists_title_date_and_dedicated_detail_links(news_store):
    user = _register()
    second_record = {
        **news_store,
        "id": "n_abcdefabcdefabcdefabcdef",
        "title": "Seconda notizia salvata",
        "meta": "Fonte: Secondo Laboratorio - Data: 2026-07-10",
        "url": "https://example.com/seconda-notizia-salvata",
        "summary": "Sintesi della seconda notizia salvata.",
        "why": "Mantiene il formato completo della card.",
        "digest_date": "2026-07-10",
    }
    assert gui.save_news_for_user(user, news_store)
    assert gui.save_news_for_user(user, second_record)

    home_view = gui.DigestView(
        None,
        "Digest principale",
        "Le fonti selezionate per oggi.",
        None,
        [gui._entry_from_news_record(news_store)],
        [],
    )
    page = gui.render_html(
        home_view,
        user=user,
        saved_sidebar_content=gui._render_saved_sidebar_panel(gui.saved_news_for_user(user)),
        current_path="/?sidebar=saved",
        active_section="saved",
    )
    sidebar = page[page.index("<aside") : page.index("</aside>")]
    main = page[page.index('<main id="canvas-container">') : page.index("</main>")]

    saved_link_end = sidebar.index("</a>", sidebar.index('href="/?sidebar=saved"')) + len("</a>")
    saved_panel_start = sidebar.index("data-saved-news-panel")
    assert saved_link_end < saved_panel_start < sidebar.index('href="/settings"')
    assert sidebar.count("data-saved-news-item") == 2
    assert "sidebar-open" in page
    assert 'class="button back-button"' not in page
    assert "saved-page-hint" not in page
    assert main.count("<article>") == 1
    assert news_store["title"] in main
    assert second_record["title"] not in main
    for record in (news_store, second_record):
        link_start = sidebar.rfind("<a", 0, sidebar.index(f'href="/news/{record["id"]}"'))
        link_end = sidebar.index("</a>", link_start)
        item = sidebar[link_start:link_end]
        assert record["title"] in item
        assert gui._format_date_for_title(record["digest_date"]) in item
        assert record["summary"] not in item
        assert record["why"] not in item
        assert f'href="/news/{record["id"]}"' in item
        assert 'target="_blank"' in item
        assert 'rel="noopener noreferrer"' in item
        assert "data-sidebar-nav" not in item


def test_saved_route_serves_the_spa_and_loads_account_data_from_the_api(news_store):
    user = _register()
    assert gui.save_news_for_user(user, news_store)
    token, _ = auth.create_session(user["id"])
    cookie = f"research_digest_session={token}"

    with _server() as address:
        status, _, page = _request(address, "GET", "/saved", headers={"Cookie": cookie})
        status_api, _, payload = _request(address, "GET", "/api/saved", headers={"Cookie": cookie})

    assert status == 200
    assert '<script type="module" src="/assets/js/app.js?v=20260722-login-chat-layout"></script>' in page
    assert 'data-ui="saved-list"' in page
    assert news_store["title"] not in page
    assert status_api == 200
    saved = json.loads(payload)
    assert saved["ok"] is True
    assert saved["items"] == [
        {
            "id": news_store["id"],
            "title": news_store["title"],
            "category": "categoria non disponibile",
            "source": "Laboratorio Test",
            "url": news_store["url"],
            "summary": news_store["summary"],
            "why": news_store["why"],
            "publication_date": "2026-07-14",
            "relevance": "n/d",
            "digest_date": "2026-07-14",
            "digest_title": "AI Research Digest - 14/07/2026",
            "saved": True,
        }
    ]


def test_saved_news_detail_card_matches_main_renderer_and_is_centered(news_store):
    user = _register()
    entry = gui._entry_from_news_record(news_store)
    record = {**news_store, "id": gui.news_entry_id(entry)}
    assert gui.save_news_for_user(user, record)
    view = gui.DigestView(None, "Digest", "Ricerca AI / modelli", None, [entry], [])

    main_page = gui.render_html(view, user=user)
    detail_page = gui.render_news_detail_html(
        record,
        user,
        current_path=f'/news/{record["id"]}',
    )
    main_article_start = main_page.index("<article>")
    main_article_end = main_page.index("</article>", main_article_start) + len("</article>")
    detail_article_start = detail_page.index("<article>")
    detail_article_end = detail_page.index("</article>", detail_article_start) + len("</article>")

    assert detail_page[detail_article_start:detail_article_end] == main_page[main_article_start:main_article_end]
    assert '<body class="news-detail-page saved-news-detail-page">' in detail_page
    assert '<section class="grid single-news-grid">' in detail_page
    assert "grid-template-columns: minmax(0, 640px)" in detail_page
    assert "justify-content: center" in detail_page
    assert ".saved-news-detail-page .single-news-grid > article" in detail_page
    assert "width: 388px" in detail_page
    assert "height: 801.383px" in detail_page
    assert "overflow-y: auto" in detail_page
    assert 'class="button back-button" href="/"' in detail_page
    assert 'aria-label="Torna alla pagina principale"' in detail_page
    assert "&#8592;</span> Indietro</a>" in detail_page


def test_history_sidebar_has_realtime_search_suggestions_and_highlighting(news_store, monkeypatch):
    user = _register()
    monkeypatch.setattr(
        gui,
        "digest_history",
        lambda: [
            gui.DigestHistoryItem(
                id="date:2026-07-14",
                path="data/digests/digest_2026-07-14.md",
                title="AI Research Digest - 14/07/2026",
                subtitle="Ricerca AI / modelli",
                updated="14/07/2026 08:00",
                key_points=[news_store["title"]],
                entry_count=1,
                digest_date="2026-07-14",
            )
        ],
    )
    monkeypatch.setattr(gui, "_history_news_records", lambda history: [news_store])

    history = gui.digest_history()
    page = gui.render_html(
        gui.DigestView(
            None,
            "Digest principale",
            "Le fonti selezionate per oggi.",
            None,
            [gui._entry_from_news_record(news_store)],
            [],
        ),
        history=history,
        user=user,
        history_search_records=gui._history_news_records(history),
        current_path="/?sidebar=open",
        show_history_sidebar=True,
    )

    assert 'id="history-search"' in page
    assert 'list="history-suggestions"' in page
    assert 'class="history-panel"' in page
    assert page.index('>Cronologia</span>') < page.index('id="history-search"')
    assert page.index('id="history-search"') < page.index('data-history-item')
    assert news_store["title"] in page
    assert "Laboratorio Test" in page
    assert "contenuto completo della ricerca" in page
    accordion_start = page.index('data-history-item')
    accordion_end = page.index("</details>", accordion_start)
    accordion = page[accordion_start:accordion_end]
    accordion_body = accordion[accordion.index('<div class="history-body">'):]
    assert 'class="history-source-list"' in accordion_body
    assert 'class="history-source-title">Laboratorio Test' in accordion_body
    assert (
        f'class="history-source-link" href="{news_store["url"]}" '
        'target="_blank" rel="noopener noreferrer"'
    ) in accordion_body
    assert (
        'class="read-more" href="/digest?date=2026-07-14" '
        'target="_blank" rel="noopener noreferrer">Leggi di pi&ugrave;'
    ) in accordion_body
    assert news_store["title"] not in accordion_body
    assert news_store["meta"] not in accordion_body
    assert news_store["summary"] not in accordion_body
    assert news_store["why"] not in accordion_body
    assert f'/news/{news_store["id"]}' not in accordion_body
    assert "data-search=" in page
    assert "data-suggestions=" in page
    assert news_store["url"] in page
    assert "const rank = (query)" in page
    assert "updateSuggestions" in page
    assert "updateSuggestions(query, ranked)" in page
    assert "JSON.parse(card.dataset.suggestions" in page
    assert "search-highlight" in page
    assert "const exactMatch = (value, phrase)" in page
    assert '(^|[^\\\\p{L}\\\\p{N}])' in page
    assert "const exactField = exactMatch(title, phrase) ? 3" in page
    assert "title.includes(phrase)" in page
    assert "terms.every((term) => content.includes(term))" in page
    assert "right.exactMatch - left.exactMatch" in page
    assert "right.exactField - left.exactField" in page
    assert "right.score - left.score" in page
    assert 'input.addEventListener("search", search)' in page
    assert "openedBeforeSearch" in page
    assert "list.insertBefore(item.card, noResults)" in page
    assert 'id="history-no-results"' in page
    assert "Nessuna notizia trovata." in page
    assert "noResults.hidden" in page
    assert "history-page-hint" not in page
    assert 'class="button back-button"' not in page


def test_history_search_metadata_includes_every_textual_record_field(news_record):
    item = gui.DigestHistoryItem(
        id="date:2026-07-14",
        path="data/digests/digest_2026-07-14.md",
        title="AI Research Digest - 14/07/2026",
        subtitle="Ricerca AI / modelli",
        updated="14/07/2026 08:00",
        key_points=[news_record["title"]],
        entry_count=1,
        digest_date="2026-07-14",
    )
    record = {**news_record, "custom_note": "Annotazione associata alla notizia"}

    metadata = gui._history_search_metadata(item, [record])
    search_text = metadata["search_text"]

    for value in (
        record["title"],
        record["meta"],
        record["url"],
        record["summary"],
        record["why"],
        record["digest_date"],
        record["custom_note"],
    ):
        assert value in search_text

    assert record["title"] in metadata["search_title"]
    assert "Laboratorio Test" in metadata["search_source"]
    assert record["title"] in metadata["search_suggestions"]
    assert "Laboratorio Test" in metadata["search_suggestions"]


@contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), gui.DigestRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request(address, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
    connection.close()
    return result


def test_history_route_serves_the_spa_and_returns_paginated_history_from_the_api(news_store, monkeypatch):
    user = _register()
    history = [
        gui.DigestHistoryItem(
            id="date:2026-07-21",
            path="data/digests/digest_2026-07-21.md",
            title="AI Research Digest - 21/07/2026",
            subtitle="Ricerca AI / modelli",
            updated="21/07/2026 08:00",
            key_points=["Nuova settimana corrente"],
            entry_count=1,
            digest_date="2026-07-21",
        ),
        gui.DigestHistoryItem(
            id="date:2026-07-14",
            path="data/digests/digest_2026-07-14.md",
            title="AI Research Digest - 14/07/2026",
            subtitle="Ricerca AI / modelli",
            updated="14/07/2026 08:00",
            key_points=[news_store["title"]],
            entry_count=1,
            digest_date="2026-07-14",
        )
    ]
    home_view = gui.DigestView(
        None,
        "Digest principale",
        "Le fonti selezionate per oggi.",
        None,
        [gui._entry_from_news_record(news_store)],
        [],
        digest_date="2026-07-14",
    )
    monkeypatch.setattr(gui, "digest_history", lambda: history)
    monkeypatch.setattr(gui, "load_digest_by_date", lambda digest_date: home_view if digest_date == "2026-07-14" else None)
    token, _ = auth.create_session(user["id"])
    cookie = f"research_digest_session={token}"

    with _server() as address:
        status, _, page = _request(address, "GET", "/history", headers={"Cookie": cookie})
        status_api, _, payload = _request(
            address,
            "GET",
            "/api/digests?limit=1&q=Laboratorio",
            headers={"Cookie": cookie},
        )

    assert status == 200
    assert '<script type="module" src="/assets/js/app.js?v=20260722-login-chat-layout"></script>' in page
    assert 'data-ui="history-list"' in page
    assert news_store["title"] not in page
    assert status_api == 200
    history_payload = json.loads(payload)
    assert history_payload["total"] == 1
    assert history_payload["limit"] == 1
    assert history_payload["next_offset"] is None
    assert history_payload["items"][0]["digest_date"] == "2026-07-14"
    assert history_payload["items"][0]["previews"][0]["title"] == news_store["title"]


def test_selected_digest_route_uses_the_api_without_server_rendering_other_weeks(news_store, monkeypatch):
    user = _register()
    selected_second = {
        "title": "Seconda notizia della settimana selezionata",
        "meta": "Fonte: Fonte Seconda - Data: 2026-07-14 - Categoria: benchmark",
        "url": "https://example.com/seconda-notizia",
        "summary": "Sintesi della seconda notizia.",
        "why": "Motivazione della seconda notizia.",
    }
    other_week = {
        "title": "Notizia di un'altra settimana",
        "meta": "Fonte: Fonte Esterna - Data: 2026-07-07 - Categoria: agenti",
        "url": "https://example.com/altra-settimana",
        "summary": "Questa notizia non deve comparire.",
        "why": "Non appartiene alla settimana selezionata.",
    }
    selected_view = gui.DigestView(
        path="data/digests/digest_2026-07-14.md",
        title="AI Research Digest - 14/07/2026",
        subtitle="Ricerca AI / modelli - 2 voci",
        warning=None,
        entries=[gui._entry_from_news_record(news_store), selected_second],
        footer=[],
        digest_date="2026-07-14",
    )
    monkeypatch.setattr(
        gui,
        "load_digest_by_date",
        lambda digest_date: selected_view if digest_date == "2026-07-14" else None,
    )
    token, _ = auth.create_session(user["id"])
    cookie = f"research_digest_session={token}"

    with _server() as address:
        status, _, page = _request(
            address,
            "GET",
            "/digest?date=2026-07-14",
            headers={"Cookie": cookie},
        )
        status_api, _, payload = _request(
            address,
            "GET",
            "/api/digest?date=2026-07-14",
            headers={"Cookie": cookie},
        )

    assert status == 200
    assert '<script type="module" src="/assets/js/app.js?v=20260722-login-chat-layout"></script>' in page
    assert 'data-ui="digest-list"' in page
    assert selected_second["title"] not in page
    assert status_api == 200
    digest = json.loads(payload)["digest"]
    assert digest["digest_date"] == "2026-07-14"
    assert [item["title"] for item in digest["items"]] == [news_store["title"], selected_second["title"]]
    assert [item["url"] for item in digest["items"]] == [news_store["url"], selected_second["url"]]
    for value in (other_week["title"], other_week["meta"], other_week["url"]):
        assert value not in page
        assert value not in payload


def test_shared_link_redirects_to_login_then_restores_news_and_can_be_saved(news_store):
    _register()
    news_id = news_store["id"]
    shared_path = f"/share/{news_id}"

    with _server() as address:
        status, headers, _ = _request(address, "GET", shared_path)
        assert status == 303
        assert headers["Location"] == f"/login?next=%2Fshare%2F{news_id}"

        login_body = urlencode(
            {
                "email": "ada@example.com",
                "password": "SecurePass1!",
                "next": shared_path,
            }
        )
        status, headers, _ = _request(
            address,
            "POST",
            "/login",
            login_body,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 303
        assert headers["Location"] == shared_path
        cookies = SimpleCookie()
        cookies.load(headers["Set-Cookie"])
        cookie = f"research_digest_session={cookies['research_digest_session'].value}"

        status, _, page = _request(address, "GET", shared_path, headers={"Cookie": cookie})
        assert status == 200
        assert news_store["title"] in page
        assert f'data-news-id="{news_id}"' in page

        status, _, response = _request(
            address,
            "POST",
            "/api/saved",
            json.dumps({"news_id": news_id, "action": "save"}),
            {"Cookie": cookie, "Content-Type": "application/json"},
        )
        assert status == 200
        assert json.loads(response)["saved"] is True

        status, _, page = _request(address, "GET", "/saved", headers={"Cookie": cookie})
        status_api, _, payload = _request(address, "GET", "/api/saved", headers={"Cookie": cookie})

    assert status == 200
    assert '<script type="module" src="/assets/js/app.js?v=20260722-login-chat-layout"></script>' in page
    assert 'data-ui="saved-list"' in page
    assert news_store["title"] not in page
    assert status_api == 200
    assert [item["id"] for item in json.loads(payload)["items"]] == [news_id]

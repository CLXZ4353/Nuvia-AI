import json
from datetime import date
from types import SimpleNamespace

import litellm
import pytest
import yaml

from src.agent import (
    _active_models_from_response,
    _candidate_publication_date,
    _fallback_models_after_empty,
    _completion_with_retries,
    _configured_models,
    _dedupe_candidate_items,
    _extract_response_message,
    _evidence_matches,
    _filter_allowed_candidate_items,
    _filter_recent_candidate_items,
    _get_function_args,
    _get_function_name,
    _ground_verified_entries,
    _message_content,
    _normalize_tool_invocation,
    _preflight_budget,
    _repair_entry_for_schema,
    _select_candidate_items,
    _compact_json,
    _compact_text,
    _validate_entries_strict,
    _tool_definitions,
    _validate_runtime_config,
    answer_news_question,
    run_agent,
)
from src.tools.dedup import DeduplicationStore


def test_tool_definitions_are_openai_compatible():
    tools = _tool_definitions()

    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "fetch_url"
    assert "parameters" in tools[0]["function"]
    assert "input_schema" not in tools[0]
    entry_schema = tools[1]["function"]["parameters"]["properties"]["entries"]["items"]
    assert "evidence" in entry_schema["required"]
    assert entry_schema["additionalProperties"] is False


def test_tool_call_argument_parsing_from_litellm_object():
    tool_call = SimpleNamespace(
        id="call_123",
        function=SimpleNamespace(
            name="submit_digest",
            arguments=json.dumps({"entries": [{"title": "Test"}]}),
        ),
    )

    assert _get_function_name(tool_call) == "submit_digest"
    assert _get_function_args(tool_call) == {"entries": [{"title": "Test"}]}


def test_normalize_gemini_submit_alias_and_single_entry_shape():
    entry = {"url": "https://arxiv.org/abs/1", "summary": "Prima frase. Seconda frase."}

    name, arguments = _normalize_tool_invocation("SubmitDigestEntries", entry)

    assert name == "submit_digest"
    assert arguments == {"entries": [entry]}


def test_extract_response_message_from_empty_choices_returns_none():
    response = SimpleNamespace(choices=[])

    assert _extract_response_message(response) is None


def test_extract_response_message_from_gemini_candidate_function_call():
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "fetch_url",
                                "args": {"url": "https://example.com/research"},
                            }
                        }
                    ]
                }
            }
        ]
    }

    message = _extract_response_message(response)

    assert message["tool_calls"][0]["function"]["name"] == "fetch_url"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "url": "https://example.com/research"
    }


def test_configured_models_includes_unique_fallbacks():
    models = _configured_models(
        {
            "model": "gemini/gemma-4-31b-it",
            "fallback_models": ["gemini/gemma-4-31b-it", "openai/gpt-4o-mini"],
        }
    )

    assert models == ["gemini/gemma-4-31b-it", "openai/gpt-4o-mini"]


def test_news_assistant_reuses_configured_gemini_and_scopes_the_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "src.agent.load_config",
        lambda _path: {
            "cost_controls": {
                "model": "gemini/gemini-2.5-flash",
                "fallback_models": ["gemini/gemma-4-31b-it"],
                "max_model_tokens": 4096,
                "model_retries": 0,
                "retry_delay_seconds": 0,
                "model_timeout_seconds": 60,
            }
        },
    )

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "Spiegazione rielaborata."}}]}

    monkeypatch.setattr("src.agent._completion_with_retries", fake_completion)

    answer = answer_news_question(
        "Spiegamela in modo semplice",
        {
            "title": "Nuovo modello AI",
            "content": "Sintesi verificata della notizia.",
            "source": "Laboratorio Test",
            "url": "https://example.com/research",
        },
        [{"role": "assistant", "content": "Che cosa vorresti approfondire di questa notizia?"}],
    )

    assert answer == "Spiegazione rielaborata."
    assert captured["model_names"] == ["gemini/gemini-2.5-flash", "gemini/gemma-4-31b-it"]
    assert captured["tools"] == []
    assert captured["max_tokens"] == 1400
    system_prompt = captured["messages"][0]["content"]
    assert "Nuovo modello AI" in system_prompt
    assert "https://example.com/research" in system_prompt
    assert "rielabora sempre con parole tue" in system_prompt
    assert "massimo di 4 righe testuali" in system_prompt
    assert "torna più volte" in system_prompt
    assert "risposta più completa e articolata" in system_prompt
    assert "Non ho informazioni disponibili!" in system_prompt
    assert captured["messages"][-1] == {"role": "user", "content": "Spiegamela in modo semplice"}


def test_successful_fallback_stays_active_for_later_iterations():
    models = [
        "gemini/gemini-2.5-flash",
        "gemini/gemma-4-26b-a4b-it",
        "gemini/gemma-4-31b-it",
    ]

    active = _active_models_from_response(models, {"model": "gemma-4-26b-a4b-it"})

    assert active == ["gemini/gemma-4-26b-a4b-it", "gemini/gemma-4-31b-it"]


def test_empty_primary_response_switches_immediately_to_gemma():
    models = ["gemini/gemini-2.5-flash", "gemini/gemma-4-26b-a4b-it"]

    assert _fallback_models_after_empty(models) == ["gemini/gemma-4-26b-a4b-it"]
    assert _fallback_models_after_empty([models[-1]]) is None


def test_completion_with_retries_uses_fallback_on_rate_limit(monkeypatch):
    calls = []

    def fake_completion(model, **kwargs):
        calls.append(model)
        if model == "gemini/gemma-4-31b-it":
            raise litellm.RateLimitError(
                message="quota exhausted",
                llm_provider="gemini",
                model=model,
            )
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("src.agent.litellm.completion", fake_completion)
    monkeypatch.setattr("src.agent.time.sleep", lambda seconds: None)

    response = _completion_with_retries(
        model_names=["gemini/gemma-4-31b-it", "openai/gpt-4o-mini"],
        messages=[],
        tools=[],
        max_tokens=100,
        model_retries=0,
        retry_delay_seconds=1,
    )

    assert response == {"choices": [{"message": {"content": "ok"}}]}
    assert calls == ["gemini/gemma-4-31b-it", "openai/gpt-4o-mini"]


def test_completion_sets_explicit_timeout(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("src.agent.litellm.completion", fake_completion)

    _completion_with_retries(
        model_names=["provider/model"],
        messages=[],
        tools=[],
        max_tokens=100,
        model_retries=0,
        retry_delay_seconds=0,
        timeout_seconds=37,
    )

    assert captured["timeout"] == 37


def test_completion_uses_model_specific_timeout(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("src.agent.litellm.completion", fake_completion)

    _completion_with_retries(
        model_names=["gemini/gemma-4-31b-it"],
        messages=[],
        tools=[],
        max_tokens=100,
        model_retries=0,
        retry_delay_seconds=0,
        timeout_seconds=60,
        model_timeouts={"gemini/gemma-4-31b-it": 120},
    )

    assert captured["timeout"] == 120


def test_completion_with_retries_uses_fallback_on_internal_server_error(monkeypatch):
    calls = []

    def fake_completion(model, **kwargs):
        calls.append(model)
        if model == "gemini/gemma-4-31b-it":
            raise litellm.InternalServerError(
                message="internal error",
                llm_provider="gemini",
                model=model,
            )
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("src.agent.litellm.completion", fake_completion)
    monkeypatch.setattr("src.agent.time.sleep", lambda seconds: None)

    response = _completion_with_retries(
        model_names=["gemini/gemma-4-31b-it", "openai/gpt-4o-mini"],
        messages=[],
        tools=[],
        max_tokens=100,
        model_retries=0,
        retry_delay_seconds=1,
    )

    assert response == {"choices": [{"message": {"content": "ok"}}]}
    assert calls == ["gemini/gemma-4-31b-it", "openai/gpt-4o-mini"]


def test_completion_with_retries_raises_clear_error_after_rate_limits(monkeypatch):
    def fake_completion(model, **kwargs):
        raise litellm.RateLimitError(
            message="quota exhausted",
            llm_provider="gemini",
            model=model,
        )

    monkeypatch.setattr("src.agent.litellm.completion", fake_completion)
    monkeypatch.setattr("src.agent.time.sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="errori transitori"):
        _completion_with_retries(
            model_names=["gemini/gemma-4-31b-it"],
            messages=[],
            tools=[],
            max_tokens=100,
            model_retries=1,
            retry_delay_seconds=1,
        )


def test_select_candidate_items_prioritizes_high_priority_sources():
    sources = [
        {"id": "low_source", "priority": "low"},
        {"id": "high_source", "priority": "high"},
    ]
    items = [
        {"source_id": "low_source", "title": "Low", "pub_date": "2026-06-18"},
        {"source_id": "high_source", "title": "High", "pub_date": "2026-06-17"},
    ]

    selected = _select_candidate_items(items, sources, max_items=1)

    assert selected == [items[1]]


def test_select_candidate_items_prefers_official_sources_over_arxiv_volume():
    sources = [
        {"id": "arxiv_ai", "priority": "high", "tier": "preprint", "max_candidates_per_run": 2},
        {"id": "official_lab", "priority": "medium", "tier": "official", "max_candidates_per_run": 4},
    ]
    items = [
        {"source_id": "arxiv_ai", "title": f"Arxiv {index}", "url": f"https://arxiv.org/abs/{index}", "pub_date": "2026-06-25"}
        for index in range(6)
    ] + [
        {
            "source_id": "official_lab",
            "title": "Official release",
            "url": "https://example.com/release",
            "pub_date": "2026-06-20",
        }
    ]

    selected = _select_candidate_items(items, sources, max_items=3)

    assert selected[0]["source_id"] == "official_lab"
    assert [item["source_id"] for item in selected].count("arxiv_ai") == 2


def test_select_candidate_items_orders_newest_items_first_within_source():
    sources = [{"id": "official_lab", "priority": "high", "tier": "official"}]
    older = {"source_id": "official_lab", "title": "Older", "url": "https://example.com/older", "pub_date": "2026-06-01"}
    newer = {"source_id": "official_lab", "title": "Newer", "url": "https://example.com/newer", "pub_date": "2026-06-25"}

    selected = _select_candidate_items([older, newer], sources, max_items=2)

    assert selected == [newer, older]


def test_select_candidate_items_limits_arxiv_source_group():
    sources = [
        {
            "id": "arxiv_ai",
            "priority": "low",
            "tier": "preprint",
            "source_group": "arxiv",
            "max_group_candidates_per_run": 1,
        },
        {
            "id": "arxiv_cl",
            "priority": "low",
            "tier": "preprint",
            "source_group": "arxiv",
            "max_group_candidates_per_run": 1,
        },
        {"id": "official_lab", "priority": "high", "tier": "official"},
    ]
    items = [
        {
            "source_id": "arxiv_ai",
            "source_group": "arxiv",
            "title": "Arxiv AI",
            "url": "https://arxiv.org/abs/1",
            "pub_date": "2026-07-02",
        },
        {
            "source_id": "arxiv_cl",
            "source_group": "arxiv",
            "title": "Arxiv CL",
            "url": "https://arxiv.org/abs/2",
            "pub_date": "2026-07-02",
        },
        {
            "source_id": "official_lab",
            "title": "Official",
            "url": "https://example.com/release",
            "pub_date": "2026-07-02",
        },
    ]

    selected = _select_candidate_items(items, sources, max_items=3)

    assert [item["source_id"] for item in selected].count("official_lab") == 1
    assert sum(1 for item in selected if item.get("source_group") == "arxiv") == 1


def test_select_candidate_items_uses_relevance_hint_within_arxiv_group():
    sources = [
        {
            "id": "arxiv_ai",
            "priority": "low",
            "tier": "preprint",
            "source_group": "arxiv",
            "max_group_candidates_per_run": 1,
        }
    ]
    generic = {
        "source_id": "arxiv_ai",
        "source_group": "arxiv",
        "title": "A generic statistical method",
        "url": "https://arxiv.org/abs/1",
        "pub_date": "2026-07-02",
    }
    relevant = {
        "source_id": "arxiv_ai",
        "source_group": "arxiv",
        "title": "RAG benchmark for agentic LLM safety evaluation",
        "url": "https://arxiv.org/abs/2",
        "pub_date": "2026-07-02",
    }

    selected = _select_candidate_items([generic, relevant], sources, max_items=2)

    assert selected == [relevant]


def test_compact_json_truncates_long_payload():
    text = _compact_json({"url": "https://example.com/" + "a" * 300}, max_length=60)

    assert len(text) == 60
    assert text.endswith("...")


def test_compact_text_collapses_and_truncates_whitespace():
    text = _compact_text("ciao\n\nmondo " + "x" * 100, max_length=30)

    assert "\n" not in text
    assert len(text) == 30
    assert text.endswith("...")


def test_message_content_supports_dict_and_object():
    assert _message_content({"content": "ok"}) == "ok"
    assert _message_content(SimpleNamespace(content="ok")) == "ok"


def test_validate_entries_strict_rejects_summary_with_one_sentence():
    entries = [
        {
            "title": "BCL per in-context learning",
            "source": "arXiv",
            "url": "https://arxiv.org/abs/2606.00000",
            "date": "2026-06-18",
            "summary": "BCL è un framework di ottimizzazione per in-context learning.",
            "perche_conta": "Aiuta i team a valutare nuovi metodi.",
            "category": "paper_ricerca",
            "relevance_score": 4,
        }
    ]

    validated = _validate_entries_strict(entries)

    assert validated == []


def test_repair_entry_does_not_pad_single_sentence_summary():
    entry = {
        "summary": "Riduce la latenza del modello mantenendo la qualità sui benchmark.",
        "perche_conta": "Consente inferenze più efficienti.",
    }

    repaired = _repair_entry_for_schema(entry)

    assert repaired["summary"].count(".") == 1
    assert repaired["perche_conta"] == "Consente inferenze più efficienti."


def test_ground_verified_entries_requires_candidate_fetch_and_literal_evidence():
    url = "https://example.com/research-result"
    evidence = "The method improves reasoning accuracy across five public benchmarks."
    entries = [
        {
            "title": "Invented title",
            "source": "Invented source",
            "url": f"{url}/?utm_source=test",
            "date": "2026-06-19",
            "summary": "Il metodo migliora il ragionamento. I risultati coprono cinque benchmark pubblici.",
            "perche_conta": "Offre un confronto riproducibile per i team AI.",
            "category": "paper_ricerca",
            "relevance_score": 5,
            "evidence": evidence,
        }
    ]
    verified = {
        url: {
            "candidate": {
                "title": "Real paper title",
                "url": url,
                "source_id": "official_lab",
                "source_name": "Official Lab",
                "pub_date": "Thu, 18 Jun 2026 08:00:00 GMT",
            },
            "content": f"Abstract. {evidence} Additional details follow.",
        }
    }

    grounded = _ground_verified_entries(entries, verified)

    assert len(grounded) == 1
    assert grounded[0]["title"] == "Real paper title"
    assert grounded[0]["source"] == "Official Lab"
    assert grounded[0]["url"] == url
    assert grounded[0]["date"] == "2026-06-18"
    assert "evidence" not in grounded[0]


def test_ground_verified_entries_normalizes_the_verified_source_title():
    url = "https://example.com/research-result"
    evidence = "The method improves reasoning accuracy across five public benchmarks."
    entries = [
        {
            "url": url,
            "summary": "Il metodo migliora il ragionamento. I risultati coprono cinque benchmark pubblici.",
            "perche_conta": "Offre un confronto riproducibile per i team AI.",
            "category": "paper_ricerca",
            "relevance_score": 5,
            "evidence": evidence,
        }
    ]
    verified = {
        url: {
            "candidate": {
                "title": "Model-X: benchmark update! v2.0",
                "url": url,
                "source_id": "official_lab",
                "source_name": "Official Lab",
                "pub_date": "Thu, 18 Jun 2026 08:00:00 GMT",
            },
            "content": f"Abstract. {evidence} Additional details follow.",
        }
    }

    grounded = _ground_verified_entries(entries, verified)

    assert grounded[0]["title"] == "Model X benchmark update v2 0"


def test_evidence_matches_allows_explicit_ellipsis_between_literal_fragments():
    source_content = (
        "A global workspace in language models Interpretability Jul 6, 2026 "
        "New interpretability research reveals an emergent mental workspace in language models. "
        "The system can route information between specialized modules during complex tasks."
    )
    evidence = (
        "A global workspace in language models Interpretability Jul 6, 2026..."
        "The system can route information between specialized modules during complex tasks."
    )

    assert _evidence_matches(evidence, source_content)


def test_evidence_matches_rejects_non_literal_paraphrase():
    source_content = "The method improves reasoning accuracy across five public benchmarks."
    evidence = "The approach substantially improves chain-of-thought ability on many evaluations."

    assert not _evidence_matches(evidence, source_content)


def test_ground_verified_entries_keeps_best_arxiv_entry_when_non_arxiv_exists():
    evidence = "The method improves reasoning accuracy across five public benchmarks."
    urls = ["https://example.com/official", "https://arxiv.org/abs/1", "https://arxiv.org/abs/2"]
    entries = [
        {
            "url": urls[0],
            "date": "2026-07-02",
            "summary": "Il report ufficiale migliora il ragionamento. I risultati coprono benchmark pubblici.",
            "perche_conta": "Offre un riferimento primario per il follow-up.",
            "category": "benchmark",
            "relevance_score": 5,
            "evidence": evidence,
        },
        {
            "url": urls[1],
            "date": "2026-07-01",
            "summary": "Il metodo migliora il ragionamento. I risultati coprono benchmark pubblici.",
            "perche_conta": "Aiuta i team a valutare nuovi metodi.",
            "category": "paper_ricerca",
            "relevance_score": 4,
            "evidence": evidence,
        },
        {
            "url": urls[2],
            "date": "2026-07-02",
            "summary": "Il modello aumenta la qualita. I risultati sono verificati su benchmark.",
            "perche_conta": "Offre un segnale piu forte per il follow-up.",
            "category": "paper_ricerca",
            "relevance_score": 5,
            "evidence": evidence,
        },
    ]
    verified = {
        urls[0]: {
            "candidate": {
                "title": "Official benchmark release",
                "url": urls[0],
                "source_id": "official_lab",
                "source_name": "Official Lab",
                "pub_date": "2026-07-02",
            },
            "content": evidence,
        },
        urls[1]: {
            "candidate": {
                "title": "Lower relevance arXiv",
                "url": urls[1],
                "source_id": "arxiv_ai",
                "source_name": "arXiv - cs.AI",
                "source_group": "arxiv",
                "pub_date": "2026-07-01",
            },
            "content": evidence,
        },
        urls[2]: {
            "candidate": {
                "title": "Higher relevance arXiv",
                "url": urls[2],
                "source_id": "arxiv_cl",
                "source_name": "arXiv - cs.CL",
                "source_group": "arxiv",
                "pub_date": "2026-07-02",
            },
            "content": evidence,
        },
    }
    sources = [
        {"id": "arxiv_ai", "source_group": "arxiv", "max_group_entries_per_digest": 1},
        {"id": "arxiv_cl", "source_group": "arxiv", "max_group_entries_per_digest": 1},
    ]

    grounded = _ground_verified_entries(entries, verified, sources)

    assert len(grounded) == 2
    assert [entry["url"] for entry in grounded] == [urls[0], urls[2]]
    assert all(not key.startswith("_") for entry in grounded for key in entry)


def test_ground_verified_entries_rejects_arxiv_below_five():
    url = "https://arxiv.org/abs/2606.00000"
    evidence = "The method improves reasoning accuracy across five public benchmarks."
    entries = [
        {
            "url": url,
            "date": "2026-07-02",
            "summary": "Il metodo migliora il ragionamento. I risultati coprono benchmark pubblici.",
            "perche_conta": "Aiuta i team a valutare nuovi metodi.",
            "category": "paper_ricerca",
            "relevance_score": 4,
            "evidence": evidence,
        }
    ]
    verified = {
        url: {
            "candidate": {
                "title": "Useful but not exceptional arXiv paper",
                "url": url,
                "source_id": "arxiv_ai",
                "source_name": "arXiv - cs.AI",
                "source_group": "arxiv",
                "pub_date": "2026-07-02",
            },
            "content": evidence,
        }
    }

    assert _ground_verified_entries(entries, verified) == []


def test_ground_verified_entries_rejects_arxiv_when_it_is_the_only_digest_source():
    url = "https://arxiv.org/abs/2606.00000"
    evidence = "The method improves reasoning accuracy across five public benchmarks."
    entries = [
        {
            "url": url,
            "date": "2026-07-02",
            "summary": "Il metodo migliora il ragionamento. I risultati coprono benchmark pubblici.",
            "perche_conta": "Aiuta i team a valutare nuovi metodi.",
            "category": "paper_ricerca",
            "relevance_score": 5,
            "evidence": evidence,
        }
    ]
    verified = {
        url: {
            "candidate": {
                "title": "Exceptional arXiv paper",
                "url": url,
                "source_id": "arxiv_ai",
                "source_name": "arXiv - cs.AI",
                "source_group": "arxiv",
                "pub_date": "2026-07-02",
            },
            "content": evidence,
        }
    }

    assert _ground_verified_entries(entries, verified) == []


def test_ground_verified_entries_rejects_source_without_verified_publication_date():
    url = "https://example.com/research-result"
    evidence = "The method improves reasoning accuracy across five public benchmarks."
    entries = [
        {
            "url": url,
            "date": "2026-07-02",
            "summary": "Il metodo migliora il ragionamento. I risultati coprono benchmark pubblici.",
            "perche_conta": "Aiuta i team a valutare nuovi metodi.",
            "category": "paper_ricerca",
            "relevance_score": 5,
            "evidence": evidence,
        }
    ]
    verified = {
        url: {
            "candidate": {
                "title": "Official research result",
                "url": url,
                "source_id": "official_lab",
                "source_name": "Official Lab",
                "pub_date": None,
            },
            "content": evidence,
        }
    }

    assert _ground_verified_entries(entries, verified) == []


def test_ground_verified_entries_drops_arxiv_duplicate_of_digest_news():
    evidence = "The release introduces a new multimodal benchmark for agent evaluation."
    official_url = "https://example.com/frontier-agent-benchmark"
    arxiv_url = "https://arxiv.org/abs/2607.00001"
    entries = [
        {
            "url": official_url,
            "date": "2026-07-02",
            "summary": "Il benchmark valuta agenti multimodali. I risultati aggiornano il confronto pubblico.",
            "perche_conta": "Offre una misura utile per scegliere modelli.",
            "category": "benchmark",
            "relevance_score": 5,
            "evidence": evidence,
        },
        {
            "url": arxiv_url,
            "date": "2026-07-02",
            "summary": "Il paper descrive lo stesso benchmark. I risultati riprendono il confronto pubblico.",
            "perche_conta": "E una conferma tecnica del benchmark.",
            "category": "paper_ricerca",
            "relevance_score": 5,
            "evidence": evidence,
        },
    ]
    verified = {
        official_url: {
            "candidate": {
                "title": "Frontier Agent Benchmark for Multimodal Evaluation",
                "url": official_url,
                "source_id": "official_lab",
                "source_name": "Official Lab",
                "pub_date": "2026-07-02",
            },
            "content": evidence,
        },
        arxiv_url: {
            "candidate": {
                "title": "Frontier Agent Benchmark for Multimodal Evaluation",
                "url": arxiv_url,
                "source_id": "arxiv_ai",
                "source_name": "arXiv - cs.AI",
                "source_group": "arxiv",
                "pub_date": "2026-07-02",
            },
            "content": evidence,
        },
    }

    grounded = _ground_verified_entries(entries, verified)

    assert len(grounded) == 1
    assert grounded[0]["url"] == official_url


@pytest.mark.parametrize(
    ("verified", "evidence", "score"),
    [
        ({}, "A sufficiently long literal excerpt from the source page.", 4),
        ("candidate", "This excerpt does not occur in the fetched source content.", 4),
        ("candidate", "A sufficiently long literal excerpt from the source page.", 2),
    ],
)
def test_ground_verified_entries_rejects_ungrounded_or_low_relevance(verified, evidence, score):
    url = "https://arxiv.org/abs/2606.00000"
    verification = {
        url: {
            "candidate": {"title": "Paper", "url": url, "source_name": "arXiv"},
            "content": "A sufficiently long literal excerpt from the source page.",
        }
    }
    entries = [{"url": url, "relevance_score": score, "evidence": evidence}]

    result = _ground_verified_entries(entries, verification if verified == "candidate" else verified)

    assert result == []


def test_candidate_filter_rejects_unlisted_domains_and_dedupes_tracking_urls():
    items = [
        {"url": "https://arxiv.org/abs/1?utm_source=x", "source_id": "a"},
        {"url": "https://arxiv.org/abs/1", "source_id": "b"},
        {"url": "https://evil.example/paper", "source_id": "c"},
    ]

    allowed = _filter_allowed_candidate_items(items, {"arxiv.org"})
    unique = _dedupe_candidate_items(allowed)

    assert unique == [{"url": "https://arxiv.org/abs/1", "source_id": "a"}]


def test_recent_candidate_filter_requires_verified_current_week_dates():
    items = [
        {"url": "https://example.com/no-date", "source_id": "static", "title": "No date", "pub_date": None},
        {"url": "https://example.com/old", "source_id": "blog", "title": "Old", "pub_date": "2026-06-28"},
        {"url": "https://example.com/monday", "source_id": "blog", "title": "Monday", "pub_date": "2026-06-29"},
        {"url": "https://example.com/today", "source_id": "blog", "title": "Today", "pub_date": "2026-07-02"},
        {"url": "https://example.com/future", "source_id": "blog", "title": "Future", "pub_date": "2026-07-03"},
    ]

    filtered = _filter_recent_candidate_items(
        items,
        {"mode": "current_week", "require_publication_date": True},
        run_date=date(2026, 7, 2),
    )

    assert [item["url"] for item in filtered] == [
        "https://example.com/monday",
        "https://example.com/today",
    ]


def test_candidate_publication_date_accepts_multiple_source_date_formats():
    assert _candidate_publication_date("02/07/2026") == date(2026, 7, 2)
    assert _candidate_publication_date("2 luglio 2026") == date(2026, 7, 2)
    assert _candidate_publication_date("Thu, 02 Jul 2026 08:00:00 GMT") == date(2026, 7, 2)
    assert _candidate_publication_date("2026.07.02") == date(2026, 7, 2)


def test_recent_candidate_filter_accepts_elastic_date_formats_but_still_requires_date():
    items = [
        {"url": "https://example.com/italian", "source_id": "blog", "pub_date": "2 luglio 2026"},
        {"url": "https://example.com/slash", "source_id": "blog", "pub_date": "02/07/2026"},
        {"url": "https://example.com/no-date", "source_id": "blog", "pub_date": None},
    ]

    filtered = _filter_recent_candidate_items(
        items,
        {"mode": "current_week", "require_publication_date": True},
        run_date=date(2026, 7, 2),
    )

    assert [item["url"] for item in filtered] == [
        "https://example.com/italian",
        "https://example.com/slash",
    ]


def test_recent_candidate_filter_supports_rolling_window():
    items = [
        {"url": "https://example.com/june-26", "source_id": "blog", "pub_date": "2026-06-26"},
        {"url": "https://example.com/june-25", "source_id": "blog", "pub_date": "2026-06-25"},
    ]

    filtered = _filter_recent_candidate_items(
        items,
        {"mode": "last_7_days", "require_publication_date": True},
        run_date=date(2026, 7, 2),
    )

    assert [item["url"] for item in filtered] == ["https://example.com/june-26"]


def test_candidate_dedup_rejects_nearly_identical_titles():
    items = [
        {"url": "https://arxiv.org/abs/1", "source_id": "a", "title": "A New Method for Efficient LLM Training"},
        {"url": "https://arxiv.org/abs/2", "source_id": "b", "title": "A new method for efficient LLM training!"},
    ]

    assert _dedupe_candidate_items(items) == [items[0]]


def test_preflight_budget_blocks_call_above_cost_cap(monkeypatch):
    monkeypatch.setattr("src.agent.litellm.token_counter", lambda **kwargs: 100)
    monkeypatch.setattr("src.agent.litellm.cost_per_token", lambda **kwargs: (0.4, 0.2))

    with pytest.raises(RuntimeError, match="Tetto di costo"):
        _preflight_budget(
            model_names=["provider/model"],
            messages=[],
            tools=[],
            requested_max_tokens=200,
            remaining_cost=0.5,
        )


def test_preflight_budget_accepts_unmapped_model_without_cost(monkeypatch):
    monkeypatch.setattr("src.agent.litellm.token_counter", lambda **kwargs: 100)
    monkeypatch.setattr(
        "src.agent.litellm.cost_per_token",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("unmapped")),
    )

    bounded_tokens, prompt_tokens, estimated_cost = _preflight_budget(
        model_names=["gemini/new-model"],
        messages=[],
        tools=[],
        requested_max_tokens=200,
        remaining_cost=1,
    )

    assert (bounded_tokens, prompt_tokens) == (200, 100)
    assert estimated_cost == 0


def test_preflight_budget_estimates_tokens_for_unknown_tokenizer(monkeypatch):
    monkeypatch.setattr(
        "src.agent.litellm.token_counter",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("unknown tokenizer")),
    )
    monkeypatch.setattr(
        "src.agent.litellm.cost_per_token",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("unmapped")),
    )

    bounded_tokens, prompt_tokens, estimated_cost = _preflight_budget(
        model_names=["custom/free-model"],
        messages=[{"role": "user", "content": "test"}],
        tools=[],
        requested_max_tokens=200,
        remaining_cost=1,
    )

    assert bounded_tokens == 200
    assert prompt_tokens > 0
    assert estimated_cost == 0


def test_preflight_budget_does_not_enforce_a_run_token_cap(monkeypatch):
    monkeypatch.setattr("src.agent.litellm.token_counter", lambda **kwargs: 100_000)
    monkeypatch.setattr("src.agent.litellm.cost_per_token", lambda **kwargs: (0, 0))

    bounded_tokens, prompt_tokens, estimated_cost = _preflight_budget(
        model_names=["gemini/gemma-4-31b-it"],
        messages=[],
        tools=[],
        requested_max_tokens=4096,
        remaining_cost=1,
    )

    assert bounded_tokens == 4096
    assert prompt_tokens == 100_000
    assert estimated_cost == 0


def test_runtime_config_rejects_wrong_beat_or_unlisted_domains():
    config = {
        "beat": "ricerca_ai_modelli",
        "source_catalog": {
            "authoritative_domains": ["arxiv.org"],
            "allow_unlisted_domains": False,
        },
        "delivery": {"primary": {"type": "markdown_file", "path": "out"}},
        "cost_controls": {"max_cost_usd_per_run": 1},
    }

    _validate_runtime_config(config)

    with pytest.raises(ValueError, match="beat"):
        _validate_runtime_config({**config, "beat": "startup"})
    with pytest.raises(ValueError, match="allow_unlisted_domains"):
        _validate_runtime_config(
            {**config, "source_catalog": {**config["source_catalog"], "allow_unlisted_domains": True}}
        )


def test_delivery_failure_does_not_mark_entry_as_seen(tmp_path, monkeypatch):
    url = "https://example.com/research-result"
    evidence = "The method improves reasoning accuracy across five public benchmarks."
    config = {
        "beat": "ricerca_ai_modelli",
        "source_catalog": {
            "path": "catalog.yaml",
            "authoritative_domains": ["example.com"],
            "allow_unlisted_domains": False,
        },
        "sources": [],
        "delivery": {
            "primary": {"type": "markdown_file", "path": "digests"},
            "failed_path": "failed",
        },
        "state": {"db_path": str(tmp_path / "seen.db"), "retention_days": 90},
        "cost_controls": {
            "model": "provider/model",
            "fallback_models": [],
            "max_cost_usd_per_run": 1,
            "max_candidate_items": 10,
            "max_agent_iterations": 3,
            "max_model_tokens": 500,
            "model_retries": 0,
            "retry_delay_seconds": 0,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    source = {"id": "official_lab", "name": "Official Lab", "url": "https://example.com", "priority": "high"}
    item = {
        "title": "Real paper title",
        "url": url,
        "source_id": "official_lab",
        "source_name": "Official Lab",
        "snippet": None,
        "pub_date": "2026-06-18",
    }
    submit_entry = {
        "title": "Ignored model title",
        "source": "Ignored model source",
        "url": url,
        "date": "2026-06-18",
        "summary": "Il metodo migliora il ragionamento. I risultati coprono cinque benchmark pubblici.",
        "perche_conta": "Offre un confronto riproducibile per i team AI.",
        "category": "paper_ricerca",
        "relevance_score": 5,
        "evidence": evidence,
    }
    responses = iter(
        [
            {
                "choices": [{"message": {"tool_calls": [_tool_call("fetch_url", {"url": url}, "fetch")]}}],
                "usage": {"total_tokens": 100},
            },
            {
                "choices": [
                    {"message": {"tool_calls": [_tool_call("submit_digest", {"entries": [submit_entry]}, "submit")]}}
                ],
                "usage": {"total_tokens": 100},
            },
        ]
    )

    monkeypatch.setattr("src.agent.resolve_sources", lambda *args: [source])
    monkeypatch.setattr("src.agent.fetch_sources", lambda *args: ([item], []))
    monkeypatch.setattr("src.agent.fetch_url", lambda *args: (True, f"Abstract. {evidence} Details."))
    monkeypatch.setattr("src.agent._preflight_budget", lambda **kwargs: (500, 100, 0.01))
    monkeypatch.setattr("src.agent._completion_with_retries", lambda **kwargs: next(responses))
    monkeypatch.setattr("src.agent._response_cost", lambda response: 0.01)
    monkeypatch.setattr("src.agent.save_failed_digest", lambda *args: tmp_path / "failed.md")
    monkeypatch.setattr("src.agent.deliver_to_file", lambda *args: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        run_agent(str(config_path))

    assert not DeduplicationStore(str(tmp_path / "seen.db")).check_already_seen(url)


def test_run_agent_retries_when_submitted_entries_all_fail_grounding(tmp_path, monkeypatch):
    url = "https://example.com/research-result"
    config = {
        "beat": "ricerca_ai_modelli",
        "source_catalog": {
            "authoritative_domains": ["example.com"],
            "allow_unlisted_domains": False,
        },
        "sources": [],
        "delivery": {
            "primary": {"type": "markdown_file", "path": "digests"},
            "failed_path": "failed",
        },
        "state": {"db_path": str(tmp_path / "seen.db"), "retention_days": 90},
        "recency": {"mode": "last_7_days", "require_publication_date": True},
        "cost_controls": {
            "model": "provider/model",
            "fallback_models": [],
            "max_cost_usd_per_run": 1,
            "max_candidate_items": 10,
            "max_agent_iterations": 4,
            "max_model_tokens": 500,
            "model_retries": 0,
            "retry_delay_seconds": 0,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    source = {"id": "official_lab", "name": "Official Lab", "url": "https://example.com", "priority": "high"}
    item = {
        "title": "Real paper title",
        "url": url,
        "source_id": "official_lab",
        "source_name": "Official Lab",
        "snippet": None,
        "pub_date": date.today().isoformat(),
    }
    bad_submit_entry = {
        "title": "Ignored model title",
        "source": "Ignored model source",
        "url": url,
        "date": date.today().isoformat(),
        "summary": "Il metodo migliora il ragionamento. I risultati coprono cinque benchmark pubblici.",
        "perche_conta": "Offre un confronto riproducibile per i team AI.",
        "category": "paper_ricerca",
        "relevance_score": 5,
        "evidence": "This excerpt is long enough but it does not appear in the fetched source.",
    }
    responses = iter(
        [
            {
                "choices": [{"message": {"tool_calls": [_tool_call("fetch_url", {"url": url}, "fetch")]}}],
                "usage": {"total_tokens": 100},
            },
            {
                "choices": [
                    {"message": {"tool_calls": [_tool_call("submit_digest", {"entries": [bad_submit_entry]}, "bad")]}},
                ],
                "usage": {"total_tokens": 100},
            },
            {
                "choices": [{"message": {"tool_calls": [_tool_call("submit_digest", {"entries": []}, "empty")]}}],
                "usage": {"total_tokens": 100},
            },
        ]
    )
    call_count = 0

    def fake_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        return next(responses)

    monkeypatch.setattr("src.agent.resolve_sources", lambda *args: [source])
    monkeypatch.setattr("src.agent.fetch_sources", lambda *args: ([item], []))
    monkeypatch.setattr("src.agent.fetch_url", lambda *args: (True, "A literal source excerpt appears here."))
    monkeypatch.setattr("src.agent._preflight_budget", lambda **kwargs: (500, 100, 0.01))
    monkeypatch.setattr("src.agent._completion_with_retries", fake_completion)
    monkeypatch.setattr("src.agent._response_cost", lambda response: 0.01)

    digest = run_agent(str(config_path))

    assert call_count == 3
    assert digest.entries == []


def test_run_agent_resolves_relative_state_path_from_config_location(tmp_path, monkeypatch):
    config_path = tmp_path / "nested" / "config.yaml"
    config_path.parent.mkdir()
    config = {
        "beat": "ricerca_ai_modelli",
        "source_catalog": {
            "authoritative_domains": ["example.com"],
            "allow_unlisted_domains": False,
        },
        "sources": [],
        "delivery": {
            "primary": {"type": "markdown_file", "path": "digests"},
            "failed_path": "failed",
        },
        "state": {"db_path": "state/seen.db", "retention_days": 90},
        "cost_controls": {
            "model": "provider/model",
            "fallback_models": [],
            "max_cost_usd_per_run": 1,
            "max_agent_iterations": 3,
            "max_model_tokens": 500,
            "model_retries": 0,
            "retry_delay_seconds": 0,
        },
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    monkeypatch.setattr(
        "src.agent.resolve_sources",
        lambda *args: [{"id": "empty", "name": "Empty", "url": "https://example.com"}],
    )
    monkeypatch.setattr("src.agent.fetch_sources", lambda *args: ([], []))

    digest = run_agent(str(config_path))

    assert digest.entries == []
    assert (config_path.parent / "state" / "seen.db").exists()
    assert (config_path.parent / "digests" / f"digest_{date.today().isoformat()}.md").exists()


def _tool_call(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }

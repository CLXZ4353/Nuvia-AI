import json
import threading
from datetime import date, datetime, time, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from src.agent import run_agent
from src.tools.dedup import DeduplicationStore


EVIDENCE = "The system improves reasoning accuracy across five reproducible public benchmarks."
TODAY = date.today()
TODAY_RFC822 = format_datetime(datetime.combine(TODAY, time(8, tzinfo=timezone.utc)))


class DigestTestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.request_paths.append(self.path)
        if self.path == "/feed.xml":
            body = f"""<?xml version="1.0"?>
            <rss version="2.0"><channel><title>Test feed</title>
            <item><title>Verified research result</title>
            <link>http://127.0.0.1:{self.server.server_port}/paper</link>
            <description>{EVIDENCE} {'Details. ' * 30}</description>
            <pubDate>{TODAY_RFC822}</pubDate></item>
            </channel></rss>"""
            content_type = "application/rss+xml"
        elif self.path == "/paper":
            self.send_error(403)
            return
        else:
            self.send_error(404)
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_end_to_end_with_mock_http_server_and_llm(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), DigestTestHandler)
    server.request_paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    article_url = f"http://127.0.0.1:{port}/paper"
    config = {
        "beat": "ricerca_ai_modelli",
        "source_catalog": {
            "authoritative_domains": [f"127.0.0.1:{port}"],
            "allow_unlisted_domains": False,
        },
        "sources": [
            {
                "id": "integration_feed",
                "name": "Integration feed",
                "type": "rss",
                "url": f"http://127.0.0.1:{port}/feed.xml",
                "trust_score": 5,
            }
        ],
        "delivery": {
            "primary": {"type": "markdown_file", "path": "digests"},
            "failed_path": "failed",
        },
        "state": {"db_path": str(tmp_path / "seen.db"), "retention_days": 90},
        "cost_controls": {
            "model": "mock/model",
            "fallback_models": [],
            "max_cost_usd_per_run": 1,
            "max_agent_iterations": 3,
            "max_model_tokens": 500,
            "model_retries": 0,
            "retry_delay_seconds": 0,
            "model_timeout_seconds": 5,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    responses = iter(
        [
            _response("fetch_url", {"url": article_url}, "fetch"),
            _response(
                "submit_digest",
                {
                    "entries": [
                        {
                            "title": "Ignored model title",
                            "source": "Ignored model source",
                            "url": article_url,
                            "date": TODAY.isoformat(),
                            "summary": "Il sistema migliora il ragionamento. I risultati coprono cinque benchmark pubblici.",
                            "perche_conta": "Fornisce un confronto riproducibile per i team AI.",
                            "category": "paper_ricerca",
                            "relevance_score": 4,
                            "evidence": EVIDENCE,
                        }
                    ]
                },
                "submit",
            ),
        ]
    )
    monkeypatch.setattr("src.agent.is_allowed_public_url", lambda *args: True)
    monkeypatch.setattr("src.tools.sources.is_allowed_public_url", lambda *args: True)
    monkeypatch.setattr("src.tools.fetch.is_allowed_public_url", lambda *args: True)
    monkeypatch.setattr("src.agent._preflight_budget", lambda **kwargs: (500, 100, 0.01))
    monkeypatch.setattr("src.agent._response_cost", lambda response: 0.01)
    monkeypatch.setattr("src.agent.litellm.completion", lambda **kwargs: next(responses))

    try:
        digest = run_agent(str(config_path))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert len(digest.entries) == 1
    assert digest.entries[0].title == "Verified research result"
    assert str(digest.entries[0].url) == article_url
    assert "/paper" not in server.request_paths
    assert list((tmp_path / "digests").glob("*.md"))
    assert DeduplicationStore(str(tmp_path / "seen.db")).check_already_seen(article_url)


def _response(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ]
                }
            }
        ],
        "usage": {"total_tokens": 100},
    }

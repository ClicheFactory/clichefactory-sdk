"""client.balance(), extract(..., return_cost=True), and the CLI's `balance` / `--show-cost`."""
from __future__ import annotations

import asyncio
import json
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import BaseModel

from clichefactory import Balance, ExtractionCost, factory
from clichefactory.errors import AuthenticationError, ConfigurationError

# The shape aio-server returns for GET /v1/billing/balance.
BALANCE_RESPONSE = {
    "tenant_id": "42",
    "balance_credits": 1240,
    "currency": "EUR",
    "low_balance_threshold_credits": 100,
    "free_extraction_pages_remaining": 3,
    "pages_remaining": {
        "extract": {
            "fast": {"credits_per_page": 10, "pages": 127},
            "balanced": {"credits_per_page": 30, "pages": 44},
            "robust": {"credits_per_page": 50, "pages": 27},
        },
        "extract_byok": {
            "fast": {"credits_per_page": 1, "pages": 1243},
            "balanced": {"credits_per_page": 3, "pages": 416},
            "robust": {"credits_per_page": 5, "pages": 251},
        },
        "to_markdown": {"credits_per_page": 20, "pages": 62},
        "to_markdown_byok": {"credits_per_page": 0, "pages": None},
    },
    "balance_usd": 12.4,  # deprecated field; must not break parsing
    "low_balance_threshold_usd": 1.0,
}


@pytest.fixture
def service(monkeypatch):
    """Route the SDK's httpx calls to a handler; records the requests."""
    requests: list[httpx.Request] = []
    state = {"status": 200, "body": BALANCE_RESPONSE}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(state["status"], json=state["body"])

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "clichefactory._service.httpx.AsyncClient",
        partial(real, transport=httpx.MockTransport(handler)),
    )
    return state, requests


def _client():
    return factory(mode="service", api_key="cf-key", base_url="http://svc.test")


# ---------- balance ----------


def test_balance_reads_the_api_keys_account(service):
    _, requests = service
    bal = _client().balance()
    assert isinstance(bal, Balance)
    assert bal.balance_credits == 1240 and bal.currency == "EUR"
    assert bal.free_extraction_pages_remaining == 3
    assert bal.pages_remaining.extract["fast"].pages == 127
    assert bal.pages_remaining.extract["balanced"].credits_per_page == 30
    assert bal.pages_remaining.to_markdown_byok.pages is None
    (req,) = requests
    assert req.method == "GET"
    assert str(req.url) == "http://svc.test/v1/billing/balance"
    assert req.headers["X-API-KEY"] == "cf-key"


def test_balance_async(service):
    bal = asyncio.run(_client().balance_async())
    assert bal.tenant_id == "42"


def test_balance_invalid_key(service):
    state, _ = service
    state.update(status=401, body={"detail": "Invalid API key"})
    with pytest.raises(AuthenticationError):
        _client().balance()


def test_balance_admin_key_has_none(service):
    state, _ = service
    state.update(status=400, body={"detail": "The admin key has no tenant of its own"})
    with pytest.raises(ConfigurationError) as exc:
        _client().balance()
    assert exc.value.info.code == "billing.no_balance"


def test_balance_needs_service_mode():
    client = factory(mode="local", model=MagicMock())
    with pytest.raises(ConfigurationError):
        client.balance()


# ---------- extract(..., return_cost=True) ----------


class _Invoice(BaseModel):
    number: str | None = None


CANONICAL_RESPONSE = {
    "status": "success",
    "result": {"number": "INV-1"},
    "metadata": {},
    "credits_used": 40,
    "pages": 4,
    "credits_per_page": 10,
    "cost_summary": {"total_cost_usd": 0.0123},  # internal; must not leak into the cost
}


def _extract(**kwargs):
    uploaded = MagicMock(file_uri="s3://bucket/uploads/mail.eml", document_id="doc-1")
    with patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock,
               return_value=uploaded), \
         patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock,
               return_value=CANONICAL_RESPONSE):
        return _client().cliche(_Invoice).extract(file="/tmp/mail.eml", **kwargs)


def test_extract_returns_the_result_alone_by_default():
    out = _extract()
    assert isinstance(out, _Invoice) and out.number == "INV-1"


def test_extract_return_cost():
    out, cost = _extract(return_cost=True)
    assert out.number == "INV-1"
    assert isinstance(cost, ExtractionCost)
    assert (cost.credits_used, cost.pages, cost.credits_per_page) == (40, 4, 10)
    assert "cost_summary" not in cost.model_dump()


def test_extract_return_cost_when_free_pages_paid():
    uploaded = MagicMock(file_uri="s3://b/k.pdf", document_id="d")
    resp = {**CANONICAL_RESPONSE, "credits_used": 0}
    with patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock,
               return_value=uploaded), \
         patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock,
               return_value=resp):
        _, cost = _client().cliche(_Invoice).extract(file="/tmp/k.pdf", return_cost=True)
    assert cost.credits_used == 0 and cost.pages == 4


def test_return_cost_refused_in_local_mode():
    client = factory(mode="local", model=MagicMock())
    with pytest.raises(ConfigurationError) as exc:
        client.cliche(_Invoice).extract(file="/tmp/x.pdf", return_cost=True)
    assert exc.value.info.code == "extract.cost_requires_service"


def test_return_cost_refused_for_text_extraction():
    with pytest.raises(ConfigurationError) as exc:
        _client().cliche(_Invoice).extract(text="INV-1", return_cost=True)
    assert exc.value.info.code == "extract.cost_requires_service"


# ---------- CLI ----------


def _run_cli(argv, capsys):
    from clichefactory.cli import main

    with patch("clichefactory.cli.load_config") as cfg:
        cfg.return_value = MagicMock(default_mode="local", api_key=None, base_url=None)
        with patch("clichefactory.cli.resolve_api_key", return_value="cf-key"), \
             patch("clichefactory.cli.resolve_base_url", return_value="http://svc.test"), \
             patch("clichefactory.cli.resolve_model", return_value=None), \
             patch("clichefactory.cli.resolve_model_api_key", return_value=None):
            main(argv)
    return capsys.readouterr()


def test_cli_balance_is_readable(service, capsys):
    out = _run_cli(["balance"], capsys).out
    assert "Balance: 1,240 credits (EUR)" in out
    assert "Free extraction pages left: 3" in out
    assert "extract         fast 127, balanced 44, robust 27" in out
    assert "to-markdown     62" in out


def test_cli_balance_json(service, capsys):
    out = _run_cli(["balance", "--json"], capsys).out
    data = json.loads(out)
    assert data["balance_credits"] == 1240
    assert data["pages_remaining"]["extract"]["fast"]["pages"] == 127


def test_cli_balance_uses_service_even_if_default_is_local(service, capsys):
    _, requests = service
    _run_cli(["balance"], capsys)
    assert len(requests) == 1


def test_cli_extract_show_cost(tmp_path, capsys):
    doc = tmp_path / "mail.eml"
    doc.write_bytes(b"Subject: hi\n\nbody")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"number": {"type": "string"}}}))
    cost = ExtractionCost(credits_used=40, pages=4, credits_per_page=10)
    with patch("clichefactory.cliche.Cliche.extract", return_value=({"number": "INV-1"}, cost)) as ex:
        res = _run_cli(["extract", str(doc), "--schema", str(schema), "--mode", "service",
                        "--show-cost"], capsys)
    assert ex.call_args.kwargs["return_cost"] is True
    assert json.loads(res.out) == {"number": "INV-1"}  # stdout stays the result
    assert "Cost: 4 pages, 40 credits (10/page)" in res.err

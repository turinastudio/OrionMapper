import httpx
import pytest
import respx

from orion_mapper.core.config import Settings
from orion_mapper.core.http import (
    AsyncHttpClient,
    MaxRetriesExceededError,
)
from orion_mapper.core.rate_limiter import TokenBucketLimiter


@pytest.mark.asyncio
async def test_http_get_json_and_text(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/data").respond(
            200, json={"status": "ok", "items": [1, 2, 3]}
        )
        respx_mock.get("/html").respond(200, text="<html><body>Hello</body></html>")

        async with AsyncHttpClient(config=test_settings) as client:
            data = await client.get_json("https://api.example.com/data")
            assert data["status"] == "ok"
            assert data["items"] == [1, 2, 3]

            text = await client.get_text("https://api.example.com/html")
            assert "Hello" in text


@pytest.mark.asyncio
async def test_http_post_request(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        route = respx_mock.post("/submit").respond(200, json={"created": True})

        async with AsyncHttpClient(config=test_settings) as client:
            resp = await client.post(
                "https://api.example.com/submit", json={"name": "test"}
            )
            assert resp.status_code == 200
            assert resp.json()["created"] is True
            assert route.called


@pytest.mark.asyncio
async def test_http_retry_on_500_server_error(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        # Fail twice with 500, 502, then succeed
        respx_mock.get("/flake").side_effect = [
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(200, text="success"),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            text = await client.get_text("https://api.example.com/flake")
            assert text == "success"


@pytest.mark.asyncio
async def test_http_max_retries_exceeded(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/fail").respond(503)

        async with AsyncHttpClient(config=test_settings) as client:
            with pytest.raises(MaxRetriesExceededError):
                await client.get("https://api.example.com/fail")


@pytest.mark.asyncio
async def test_http_retry_after_header_handling(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/rate-limited").side_effect = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"result": "ok"}),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            data = await client.get_json("https://api.example.com/rate-limited")
            assert data["result"] == "ok"


@pytest.mark.asyncio
async def test_http_network_error_retries(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/network-drop").side_effect = [
            httpx.ConnectError("Connection refused"),
            httpx.Response(200, text="reconnected"),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            text = await client.get_text("https://api.example.com/network-drop")
            assert text == "reconnected"


@pytest.mark.asyncio
async def test_http_non_retryable_404(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        route = respx_mock.get("/missing").respond(404, text="Not Found")

        async with AsyncHttpClient(config=test_settings) as client:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await client.get("https://api.example.com/missing")
            assert exc_info.value.response.status_code == 404
            # Should NOT retry 404
            assert route.call_count == 1


@pytest.mark.asyncio
async def test_http_client_with_rate_limiter(test_settings: Settings):
    limiter = TokenBucketLimiter(rate=50.0, capacity=10.0)
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/throttled").respond(200, json={"ok": True})

        async with AsyncHttpClient(
            config=test_settings, rate_limiter=limiter
        ) as client:
            res = await client.get_json("https://api.example.com/throttled")
            assert res["ok"] is True

            # Also test per-request rate_limiter override
            per_req_limiter = TokenBucketLimiter(rate=50.0, capacity=10.0)
            res2 = await client.get(
                "https://api.example.com/throttled", rate_limiter=per_req_limiter
            )
            assert res2.status_code == 200


@pytest.mark.asyncio
async def test_http_retry_after_non_digit(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/rate-limited-date").side_effect = [
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200, json={"result": "ok"}),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            data = await client.get_json("https://api.example.com/rate-limited-date")
            assert data["result"] == "ok"


@pytest.mark.asyncio
async def test_http_multiple_network_errors_succeeds(test_settings: Settings):
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/unstable").side_effect = [
            httpx.ConnectTimeout("Timeout 1"),
            httpx.ReadTimeout("Timeout 2"),
            httpx.Response(200, text="finally"),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            text = await client.get_text("https://api.example.com/unstable")
            assert text == "finally"


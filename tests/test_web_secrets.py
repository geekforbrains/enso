"""Secret forms expose names only and enforce the shared web write boundary."""

import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from enso import maintenance, secrets
from enso.web.server import create_app


@pytest.fixture
async def client(enso_home):
    async with TestClient(TestServer(create_app(enso_home))) as client:
        yield client


async def form(client):
    response = await client.get("/secrets")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = await response.text()
    token = re.search(r'name="_csrf" value="([^"]+)"', body)[1]
    return {"_csrf": token, "name": "TOKEN", "value": "private-web-value\r\nsecond\n"}


async def test_web_create_list_duplicate_delete_without_chat(client, enso_home):
    data = await form(client)
    response = await client.post("/secrets", data=data, allow_redirects=False)
    assert response.status == 303 and response.headers["Location"] == "/secrets"
    assert response.headers["Cache-Control"] == "no-store"
    assert secrets.resolve(enso_home, ["TOKEN"])["TOKEN"] == data["value"]
    for response in (await client.get("/secrets"), await client.post("/secrets", data=data)):
        body = await response.text()
        assert "TOKEN" in body and "private-web-value" not in body
        assert 'value="private' not in body and 'name="edit"' not in body
    response = await client.post("/secrets/TOKEN/delete", data={"_csrf": data["_csrf"]})
    assert response.status == 200 and secrets.names(enso_home) == []
    assert not enso_home.config.exists()  # no configured chat or daemon required


@pytest.mark.parametrize(
    "attack", ["absent", "wrong", "duplicate", "cross_origin", "cross_site", "null_origin"]
)
async def test_form_writes_reject_cross_site_or_missing_token(client, enso_home, attack):
    data = await form(client)
    headers = {}
    if attack == "absent":
        del data["_csrf"]
    elif attack == "wrong":
        data["_csrf"] = "wrong"
    elif attack == "duplicate":
        data = [*data.items(), ("_csrf", data["_csrf"])]
    elif attack == "cross_origin":
        headers["Origin"] = "https://attacker.example"
    elif attack == "null_origin":
        headers["Origin"] = "null"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    response = await client.post("/secrets", data=data, headers=headers)
    assert response.status == 403
    assert "private-web-value" not in await response.text()
    assert secrets.names(enso_home) == []


async def test_same_origin_write_and_delete_protection(client, enso_home):
    data = await form(client)
    response = await client.post(
        "/secrets", data=data, headers={"Origin": str(client.make_url("/")).rstrip("/")}
    )
    assert response.status == 200
    assert (await client.post("/secrets/TOKEN/delete", data={})).status == 403
    assert secrets.names(enso_home) == ["TOKEN"]


async def test_bad_value_never_returns_in_html(client):
    data = await form(client)
    data["value"] += "\x00"
    response = await client.post("/secrets", data=data)
    assert response.status == 400
    assert "private-web-value" not in await response.text()


async def test_only_explicit_routes_can_write(client):
    data = await form(client)
    for method, path in (
        ("POST", "/health"),
        ("PUT", "/secrets"),
        ("DELETE", "/secrets"),
        ("GET", "/secrets/TOKEN/delete"),
    ):
        assert (await client.request(method, path, data=data)).status == 405
    assert (await client.post("/missing", data=data)).status == 404
    assert (await client.post("/secrets", json=data)).status == 415


async def test_web_writes_obey_home_maintenance(client, enso_home):
    data = await form(client)
    maintenance.write_json(enso_home.maintenance, {"reason": "update"})
    assert (await client.post("/secrets", data=data)).status == 503
    assert not enso_home.db.exists()
    enso_home.maintenance.unlink()
    maintenance.write_json(enso_home.runtime_dir / "development.json", {})
    with maintenance.exclusive_access(enso_home, timeout=0):
        assert (await client.post("/secrets", data=data)).status == 503
    assert (await client.post("/secrets", data=data)).status == 200


async def test_safari_opaque_origin_requires_same_origin_and_token(client):
    data = await form(client)
    headers = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    assert (await client.post("/secrets", data=data, headers=headers)).status == 200
    assert (await client.post("/secrets/TOKEN/delete", data={}, headers=headers)).status == 403

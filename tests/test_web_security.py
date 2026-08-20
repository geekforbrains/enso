"""Security-boundary tests for state-changing dashboard requests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")

from starlette.testclient import TestClient

from enso.secret_refs import SecretResolutionError
from enso.web import app as web_app


def _client(
    tmp_path: Path,
    monkeypatch,
    web_config: dict | None = None,
) -> TestClient:
    workspace = tmp_path / "workspaces" / "default"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(tmp_path))
    runtime = SimpleNamespace(
        config={
            "web": web_config or {},
            "workspaces": {
                "default": {
                    "policy": "admin",
                    "concurrency": 1,
                }
            },
            "policies": {
                "admin": {
                    "unrestricted": True,
                    "providers": ["claude"],
                    "default_provider": "claude",
                    "chat_commands": "*",
                }
            },
        },
    )
    return TestClient(web_app.create_app(runtime), base_url="http://127.0.0.1")


def test_write_forms_include_process_scoped_csrf_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/agents")

    assert response.status_code == 200
    assert (
        f'name="_csrf" value="{client.app.state.csrf_token}"'
        in response.text
    )
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_every_post_route_is_csrf_protected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    post_routes = [
        route
        for route in client.app.routes
        if "POST" in (getattr(route, "methods", None) or set())
    ]

    assert post_routes
    assert all(
        getattr(route.endpoint, "_csrf_protected", False)
        for route in post_routes
    )


def test_arbitrary_host_cannot_read_token_or_submit_write(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch).app
    attacker = TestClient(app, base_url="http://attacker.example")

    get_response = attacker.get("/agents")
    post_response = attacker.post(
        "/agents/edit",
        data={
            "content": "rebound instructions",
            "_csrf": app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert get_response.status_code == 400
    assert app.state.csrf_token not in get_response.text
    assert post_response.status_code == 400
    assert not (tmp_path / "AGENTS.md").exists()


def test_configured_allowed_host_permits_named_remote_host_only(tmp_path, monkeypatch):
    client = _client(
        tmp_path,
        monkeypatch,
        {
            "host": "0.0.0.0",
            "allowed_hosts": ["enso.example.test", "*"],
        },
    )
    app = client.app
    allowed = TestClient(app, base_url="http://enso.example.test")
    arbitrary = TestClient(app, base_url="http://attacker.example")

    assert allowed.get("/agents").status_code == 200
    assert arbitrary.get("/agents").status_code == 400


def test_shared_token_bootstraps_http_only_cookie(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, {"token": "correct-horse"})

    assert client.get("/agents").status_code == 401
    assert client.get("/health").status_code == 200

    login = client.get("/agents?token=correct-horse")

    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    assert client.get("/agents").status_code == 200


def test_1password_web_token_takes_precedence_over_literal(
    tmp_path, monkeypatch,
):
    helper = tmp_path / "1password.sh"
    helper.write_text(
        'op_secret() { printf "resolved-web-token\\n"; }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("enso.secret_refs.ONEPASSWORD_HELPER", str(helper))
    client = _client(
        tmp_path,
        monkeypatch,
        {
            "token": "stale-literal-token",
            "token_1password": {
                "item": "Enso - Web - Dashboard",
                "field": "WEB_TOKEN",
            },
        },
    )

    assert client.get("/agents?token=stale-literal-token").status_code == 401
    assert client.get("/agents?token=resolved-web-token").status_code == 200


def test_invalid_1password_web_token_reference_fails_app_creation(tmp_path):
    runtime = SimpleNamespace(
        config={
            "web": {
                "token": "stale-literal-token",
                "token_1password": {
                    "item": "Enso - Web - Dashboard",
                    "field": "",
                },
            },
            "workspaces": {},
            "policies": {},
        },
    )

    with pytest.raises(SecretResolutionError, match=r"token_1password\.field"):
        web_app.create_app(runtime)


@pytest.mark.parametrize("token", [None, True, 123, {}, ["token"]])
def test_invalid_literal_web_token_fails_app_creation(tmp_path, token):
    runtime = SimpleNamespace(
        config={"web": {"token": token}, "workspaces": {}, "policies": {}},
    )

    with pytest.raises(SecretResolutionError, match="token must be a string"):
        web_app.create_app(runtime)


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_agents_edit_rejects_missing_or_invalid_csrf_token(
    tmp_path, monkeypatch, token
):
    client = _client(tmp_path, monkeypatch)
    data = {"content": "attacker instructions"}
    if token is not None:
        data["_csrf"] = token

    response = client.post(
        "/agents/edit",
        data=data,
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert not (tmp_path / "AGENTS.md").exists()


def test_agents_edit_accepts_valid_csrf_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agents/edit",
        data={
            "content": "trusted instructions",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (tmp_path / "AGENTS.md").read_text() == "trusted instructions"


def test_agents_view_reads_install_instructions_not_workspace_instructions(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    (tmp_path / "AGENTS.md").write_text("shared instructions", encoding="utf-8")
    workspace_agents = tmp_path / "workspaces" / "default" / "AGENTS.md"
    workspace_agents.write_text("workspace instructions", encoding="utf-8")

    response = client.get("/agents")

    assert response.status_code == 200
    assert "shared instructions" in response.text
    assert "workspace instructions" not in response.text

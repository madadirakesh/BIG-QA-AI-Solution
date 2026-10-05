"""
auth_manager.py
----------------
Centralised authentication handling for the framework so individual
locustfiles don't need to re-implement login/token logic.

Supported auth types (configured in config/auth_config.yaml):

    none            - no auth
    basic           - HTTP Basic Auth (username/password)
    api_key         - static API key sent as a header
    bearer_static   - a static bearer token sent as a header
    bearer_login    - POST credentials to a login endpoint, extract a token
                       from the JSON response, send it as a Bearer token on
                       every subsequent request. Supports token expiry +
                       automatic re-login.
    oauth2_client   - OAuth2 client-credentials grant against a token URL

Usage inside a locustfile:

    from core.auth_manager import AuthManager

    auth = AuthManager.from_config("config/auth_config.yaml")

    class MyUser(HttpUser):
        def on_start(self):
            self.headers = auth.get_headers(self.client)
"""

import time
import base64
import yaml
import requests


class AuthManager:
    def __init__(self, config: dict):
        self.config = config or {"type": "none"}
        self.auth_type = self.config.get("type", "none")
        self._token = None
        self._token_expiry = 0  # epoch seconds

    @classmethod
    def from_config(cls, path):
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cls(cfg)

    def get_headers(self, http_client=None):
        """
        Returns a dict of headers to attach to every request.
        `http_client` (optional) is a requests-compatible session/client,
        used only for auth types that need to make a login call
        (e.g. Locust's self.client inside an HttpUser).
        """
        if self.auth_type == "none":
            return {}

        if self.auth_type == "basic":
            user = self.config["username"]
            pwd = self.config["password"]
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            return {"Authorization": f"Basic {token}"}

        if self.auth_type == "api_key":
            header_name = self.config.get("header_name", "X-API-Key")
            return {header_name: self.config["api_key"]}

        if self.auth_type == "bearer_static":
            return {"Authorization": f"Bearer {self.config['token']}"}

        if self.auth_type == "bearer_login":
            self._ensure_valid_token(http_client)
            return {"Authorization": f"Bearer {self._token}"}

        if self.auth_type == "oauth2_client":
            self._ensure_valid_token(http_client)
            return {"Authorization": f"Bearer {self._token}"}

        raise ValueError(f"Unsupported auth type: {self.auth_type}")

    def _ensure_valid_token(self, http_client):
        if self._token and time.time() < self._token_expiry - 5:
            return  # still valid, refresh 5s before real expiry
        if self.auth_type == "bearer_login":
            self._login(http_client)
        elif self.auth_type == "oauth2_client":
            self._oauth2_login()

    def _login(self, http_client):
        cfg = self.config
        payload = {cfg["username_field"]: cfg["username"], cfg["password_field"]: cfg["password"]}
        client = http_client or requests
        resp = client.post(cfg["login_url"], json=payload, name="auth_login")
        resp.raise_for_status()
        data = resp.json()
        self._token = _extract_from_path(data, cfg.get("token_path", "token"))
        ttl = cfg.get("token_ttl_seconds", 3600)
        self._token_expiry = time.time() + ttl

    def _oauth2_login(self):
        cfg = self.config
        resp = requests.post(
            cfg["token_url"],
            data={
                "grant_type": "client_credentials",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "scope": cfg.get("scope", ""),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        ttl = data.get("expires_in", 3600)
        self._token_expiry = time.time() + ttl


def _extract_from_path(data, dotted_path):
    """Extract a nested value, e.g. 'data.token' -> data['data']['token']"""
    value = data
    for key in dotted_path.split("."):
        value = value[key]
    return value

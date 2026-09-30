"""Reusable Docmost login sessions, scoped by instance URL and account profile."""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .engine import APIError, DocmostClient
from .markdown_tree import PublisherError
from .storage import config_directory, private_directory, read_private_json, write_private_json

LOG = logging.getLogger("docmost-publisher")
COOKIE_FIELDS = (
    "version", "name", "value", "port", "port_specified", "domain",
    "domain_specified", "domain_initial_dot", "path", "path_specified",
    "secure", "expires", "discard", "comment", "comment_url", "rfc2109",
)


def token_expiry(cookie: Cookie) -> int | None:
    """Unverified JWT exp is informational only; the server validates the token."""
    values = [cookie.expires] if cookie.expires else []
    try:
        encoded = cookie.value.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expiry = payload.get("exp")
        if type(expiry) is int and expiry > 0:
            values.append(expiry)
    except (ValueError, IndexError, TypeError, AttributeError):
        pass
    return min(values) if values else None


class SessionStore:
    def __init__(self, base_url: str, profile: str = "default", directory: Path | None = None):
        self.base_url, self.profile = base_url, profile
        self.directory = directory or config_directory()
        key = hashlib.sha256((base_url + "\n" + profile).encode()).hexdigest()
        self.path = self.directory / "sessions" / f"{key}.json"
        self.email = ""
        self._signature: str | None = None

    def prepare(self) -> None:
        private_directory(self.directory)
        private_directory(self.path.parent)

    def load(self, client: DocmostClient) -> bool:
        self.prepare()
        record = read_private_json(self.path)
        if record is None:
            return False
        if record.get("version") != 1 or record.get("url") != self.base_url or record.get("profile") != self.profile:
            raise PublisherError("Stored session belongs to another instance/profile or has an unsupported format")
        fields = record.get("cookie")
        if not isinstance(fields, dict) or set(fields) != set(COOKIE_FIELDS) | {"rest"}:
            raise PublisherError("Invalid saved cookie; remove the token file and log in again")
        if fields.get("name") != "authToken" or not isinstance(fields.get("value"), str) or not fields["value"]:
            raise PublisherError("Stored session does not contain an authToken cookie")
        if any(ord(c) < 33 or ord(c) > 126 or c in ";," for c in fields["value"]):
            raise PublisherError("Invalid characters in saved session token")
        domain = fields.get("domain")
        host = urlsplit(self.base_url).hostname or ""
        if not isinstance(domain, str) or not domain:
            raise PublisherError("Saved cookie must have an explicit domain")
        # CookieJar represents localhost as localhost.local.
        applicable = domain.lstrip(".") in {host, host + ".local"}
        if fields.get("domain_specified") and host.endswith("." + domain.lstrip(".")):
            applicable = True
        if not applicable:
            raise PublisherError("Saved cookie domain does not match the configured Docmost instance")
        try:
            cookie = Cookie(**fields)
        except (TypeError, ValueError) as exc:
            raise PublisherError("Invalid saved session cookie") from exc
        expiry = token_expiry(cookie)
        if expiry is not None and expiry <= time.time() + 10:
            LOG.info("Cached session has expired; login is required.")
            return False
        client.session.cookies.set_cookie(cookie)
        client.assert_cookie_scope()
        client.logged_in = True
        self.email = record.get("email", "")
        self._signature = json.dumps(fields, sort_keys=True)
        return True

    def save(self, client: DocmostClient, email: str | None = None) -> None:
        cookies = [c for c in client.session.cookies if c.name == "authToken"]
        if not cookies:
            return
        if len(cookies) != 1:
            raise PublisherError("Multiple authToken cookies were returned; refusing an ambiguous session")
        cookie = cookies[0]
        fields = {name: getattr(cookie, name) for name in COOKIE_FIELDS}
        fields["rest"] = {"HttpOnly": None}
        if email is not None:
            self.email = email
        signature = json.dumps(fields, sort_keys=True)
        if signature == self._signature:
            return
        self.prepare()
        write_private_json(self.path, {
            "version": 1, "url": self.base_url, "profile": self.profile,
            "email": self.email, "saved_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": token_expiry(cookie), "cookie": fields,
        })
        self._signature = signature

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
        self._signature = None


def credentials(config: dict, *, interactive: bool = True, explicit_email: str | None = None) -> tuple[str, str]:
    email = explicit_email or config["email"] or os.environ.get(config["email_env"], "")
    password = os.environ.get(config["password_env"], "")
    if interactive and sys.stdin.isatty():
        email = email or input("Docmost account email: ").strip()
        password = password or getpass.getpass("Docmost password (not stored): ")
    if not email or not password:
        raise PublisherError(
            f"No valid cached session. Run docmost-publish login interactively, or provide "
            f"{config['email_env']} and {config['password_env']} in the environment."
        )
    return email, password


def authenticate(client: DocmostClient, store: SessionStore, config: dict, *,
                 force_login: bool = False, interactive: bool = True,
                 explicit_email: str | None = None) -> list[dict]:
    """Validate a cached session before writes. Never retry a failed write after login."""
    restored = store.load(client)
    desired_email = explicit_email or config["email"] or os.environ.get(config["email_env"], "")
    if restored and desired_email and desired_email.casefold() != store.email.casefold() and not force_login:
        raise PublisherError("Cached session belongs to another account. Use a different auth.profile or run login.")
    if restored and not force_login:
        try:
            spaces = client.spaces()
        except APIError as exc:
            if exc.status != 401:
                raise
            client.session.cookies.clear()
            store.remove()
            LOG.info("Server rejected the cached session; login is required.")
        else:
            LOG.info("Using cached session for %s (profile %s)", client.base_url, store.profile)
            store.save(client)
            client.cookie_changed = lambda: store.save(client)
            return spaces
    elif restored:
        # Explicit login refreshes/replaces this profile and revokes the old session.
        try:
            client.logout()
        except APIError as exc:
            if exc.status != 401:
                raise
        store.remove()
    client.session.cookies.clear()
    email, password = credentials(config, interactive=interactive, explicit_email=explicit_email)
    client.login(email, password)
    del password
    spaces = client.spaces()
    store.save(client, email)
    client.cookie_changed = lambda: store.save(client)
    LOG.info("Session saved to %s (permissions 0600)", store.path)
    return spaces


def describe_session(store: SessionStore) -> str:
    record = read_private_json(store.path)
    if record is None:
        return f"No cached session for {store.base_url} (profile {store.profile})."
    expiry = record.get("expires_at")
    try:
        date = datetime.fromtimestamp(expiry, timezone.utc).isoformat() if expiry else "not advertised"
    except (ValueError, TypeError, OverflowError):
        date = "unknown"
    return (
        f"Instance: {store.base_url}\nProfile: {store.profile}\n"
        f"Account: {record.get('email', '')}\nToken file: {store.path}\n"
        f"Reported expiry (UTC): {date}\n"
        "Local metadata only; this does not prove that the server still accepts the session."
    )

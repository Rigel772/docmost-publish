#!/usr/bin/env python3
"""Rebuild one Docmost page subtree from a local Markdown directory.

Reconciliation engine for an explicitly scoped, destructive page-tree publisher.
Uses ordinary session login, not API keys, SQL, or enterprise functionality.
See README.md for the version-dependent internal API contract and limitations.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import getpass
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urljoin, urlsplit

import requests

from .markdown_tree import PublisherError, SourceTree, Asset, PageSource, clean_title, load_yaml

LOG = logging.getLogger("docmost-publisher")
EMPTY_DOC = {"type": "doc", "content": [{"type": "paragraph"}]}
DEFAULTS = {
    "version": 1,
    "docmost": {
        "url": "",
        "auth": {"method": "session", "profile": "default", "email": "", "email_env": "DOCMOST_EMAIL", "password_env": "DOCMOST_PASSWORD"},
        "space": "", "space_id": None,
        "root": {"title": "", "id": None},
        "http": {"allow_http": False, "ca_bundle": None, "connect_timeout": 10,
                 "read_timeout": 90, "request_delay_ms": 100},
    },
    "source": {
        "path": ".", "index_file": "README.md",
        "exclude": ["**/node_modules/**", "**/vendor/**"],
        "max_markdown_bytes": 5 * 1024 * 1024,
        "max_total_bytes": 256 * 1024 * 1024,
    },
    "publish": {
        "mode": "replace_tree", "preserve_root": True, "deletion": "trash",
        "allow_empty": False, "max_pages": 2000, "max_delete_pages": 2000,
        "max_depth": 40, "verify": True, "verify_attempts": 10,
        "preflight_write_probe": True,
    },
    "links": {"fragment_policy": "preserve"},
    "assets": {
        "enabled": True, "max_bytes": 20 * 1024 * 1024,
        "allowed_extensions": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".pdf", ".txt", ".csv"],
    },
}


def merge_config(defaults: dict, supplied: Any, path: str = "config") -> dict:
    if not isinstance(supplied, dict):
        raise PublisherError(f"{path} must be a mapping")
    unknown = set(supplied) - set(defaults)
    if unknown:
        raise PublisherError(f"Unknown keys in {path}: {', '.join(map(str, sorted(unknown, key=str)))}")
    result = copy.deepcopy(defaults)
    for key, value in supplied.items():
        original = defaults[key]
        if isinstance(original, dict):
            result[key] = merge_config(original, value, f"{path}.{key}")
        elif original is None:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise PublisherError(f"{path}.{key} must be a nonempty string or null")
            result[key] = value
        elif type(value) is not type(original):
            raise PublisherError(f"Invalid type for {path}.{key}; expected {type(original).__name__}")
        else:
            result[key] = value
    return result


def read_config(path: Path, *, require_destination: bool = True) -> dict:
    try:
        value = load_yaml(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PublisherError(f"Cannot read configuration: {path}") from exc
    cfg = merge_config(DEFAULTS, value)
    return validate_config(cfg, path, require_destination=require_destination)


def validate_config(cfg: dict, path: Path, *, require_destination: bool = True) -> dict:
    dm, source, pub = cfg["docmost"], cfg["source"], cfg["publish"]
    if cfg["version"] != 1:
        raise PublisherError("Only configuration version 1 is supported")
    if dm["auth"]["method"] != "session":
        raise PublisherError("This publisher supports auth.method: session only")
    if require_destination and not dm["space"] and not dm["space_id"]:
        raise PublisherError("Set docmost.space (exact name) or docmost.space_id")
    if require_destination and not clean_title(dm["root"]["title"]):
        raise PublisherError("Set docmost.root.title")
    dm["root"]["title"] = clean_title(dm["root"]["title"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", dm["auth"]["profile"]):
        raise PublisherError("auth.profile must contain 1-80 letters, digits, dots, underscores, or hyphens")
    dm["url"] = dm["url"].rstrip("/")
    u = urlsplit(dm["url"])
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise PublisherError("docmost.url must be an HTTP(S) instance URL without credentials, query, or fragment")
    if u.path.rstrip("/").endswith("/api"):
        raise PublisherError("docmost.url must not include the /api suffix")
    if u.scheme == "http" and not dm["http"]["allow_http"]:
        raise PublisherError("Plain HTTP would expose credentials; use HTTPS or explicitly set http.allow_http: true")
    if pub["mode"] != "replace_tree" or not pub["preserve_root"] or pub["deletion"] != "trash":
        raise PublisherError("Supported publication settings: replace_tree, preserve_root: true, deletion: trash")
    if cfg["links"]["fragment_policy"] not in {"preserve", "drop"}:
        raise PublisherError("links.fragment_policy must be preserve or drop")
    if Path(source["index_file"]).name != source["index_file"] or Path(source["index_file"]).suffix.lower() not in {".md", ".markdown"}:
        raise PublisherError("source.index_file must be a Markdown filename, not a path")
    for field in [source["exclude"], cfg["assets"]["allowed_extensions"]]:
        if not all(isinstance(x, str) and x for x in field):
            raise PublisherError("Exclude patterns and attachment extensions must be nonempty strings")
    for section, keys in [(source, ["max_markdown_bytes", "max_total_bytes"]),
                          (pub, ["max_pages", "max_depth", "verify_attempts"]),
                          (cfg["assets"], ["max_bytes"]),
                          (dm["http"], ["connect_timeout", "read_timeout"])]:
        if any(section[k] < 1 for k in keys):
            raise PublisherError("Size, depth, timeout, and attempt limits must be positive integers")
    if pub["max_delete_pages"] < 0 or dm["http"]["request_delay_ms"] < 0:
        raise PublisherError("Deletion limit and request delay cannot be negative")
    cfg["assets"]["allowed_extensions"] = [x.lower() for x in cfg["assets"]["allowed_extensions"]]
    if any(not x.startswith(".") or "/" in x for x in cfg["assets"]["allowed_extensions"]):
        raise PublisherError("Asset extensions must have forms such as .png or .pdf")
    if dm["http"]["ca_bundle"]:
        ca = (path.parent / dm["http"]["ca_bundle"]).resolve()
        if not ca.is_file():
            raise PublisherError(f"CA bundle not found: {ca}")
        dm["http"]["ca_bundle"] = str(ca)
    return cfg


def require_record(value: Any, context: str) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        raise PublisherError(f"Unexpected {context} response: missing record ID; no further writes will be attempted")
    return value


class APIError(PublisherError):
    def __init__(self, status: int, endpoint: str, message: str):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint


class DocmostClient:
    def __init__(self, config: dict):
        self.config = config
        self.base_url = config["url"]
        self.cookie_changed = None
        self.session = requests.Session()
        # Do not let ~/.netrc silently substitute HTTP Basic credentials.
        # Set proxies explicitly in code when a deployment actually requires them.
        self.session.trust_env = False
        self.session.verify = config["http"]["ca_bundle"] or True
        origin = urlsplit(self.base_url)
        self.session.headers.update({"Accept": "application/json", "User-Agent": "docmost-folder-publisher/1.0.0",
                                     "Origin": f"{origin.scheme}://{origin.netloc}", "Referer": self.base_url + "/"})
        self.logged_in = False
        self.last_request = 0.0

    def post(self, endpoint: str, payload: dict | None = None, *, files: Any = None,
             data: Any = None, read_only: bool = False) -> Any:
        # No automatic retry, including on timeouts: create/upload/delete outcomes
        # can be ambiguous. Read polling is performed explicitly by the caller.
        delay = self.config["http"]["request_delay_ms"] / 1000
        remaining = delay - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        try:
            kwargs = {"files": files, "data": data} if files is not None else {"json": payload or {}}
            response = self.session.post(
                self.base_url + "/api/" + endpoint.lstrip("/"),
                timeout=(self.config["http"]["connect_timeout"], self.config["http"]["read_timeout"]),
                allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            detail = "Read failed." if read_only else "Outcome may be unknown; request was not retried."
            raise PublisherError(f"Network failure on {endpoint}. {detail}") from exc
        finally:
            self.last_request = time.monotonic()
        if self.cookie_changed is not None:
            self.cookie_changed()
        with response:
            if 300 <= response.status_code < 400:
                raise PublisherError(f"Redirect on {endpoint}; configure the final instance URL (redirects are not followed)")
            if not 200 <= response.status_code < 300:
                hint = {401: "Session/login rejected", 403: "Insufficient permissions or endpoint unavailable",
                        404: "Endpoint/record not found", 413: "Payload exceeds server/proxy limit",
                        429: "Rate limited; reduce request rate"}.get(response.status_code, "Request failed")
                raise APIError(response.status_code, endpoint, f"HTTP {response.status_code} on {endpoint}: {hint}. Not retried.")
            if not response.content.strip():
                return None
            try:
                body = response.json()
            except ValueError as exc:
                raise PublisherError(f"Non-JSON response on {endpoint}; check reverse proxy and instance URL") from exc
        if isinstance(body, dict) and body.get("success") is False:
            raise PublisherError(f"Server reported an unsuccessful operation on {endpoint}")
        # Standard endpoints use a data envelope; file uploads return a bare object.
        return body["data"] if isinstance(body, dict) and "data" in body and "id" not in body else body

    def login(self, email: str, password: str) -> None:
        result = self.post("auth/login", {"email": email, "password": password})
        if isinstance(result, dict) and any(result.get(k) for k in ["userHasMfa", "requiresMfaSetup", "requiresMfa"]):
            raise PublisherError("Interactive MFA is required; this publisher does not automate or bypass MFA/SSO")
        cookies = [c for c in self.session.cookies if c.name == "authToken"]
        if not cookies:
            raise PublisherError("Login did not return an authToken cookie; check credentials, SSO/MFA, and Docmost version")
        self.logged_in = True
        prepared = self.session.prepare_request(requests.Request("POST", self.base_url + "/api/spaces"))
        if "authToken=" not in prepared.headers.get("Cookie", ""):
            raise PublisherError("Session cookie cannot be sent to the configured URL; check HTTPS and cookie domain")

    def assert_cookie_scope(self) -> None:
        prepared = self.session.prepare_request(requests.Request("POST", self.base_url + "/api/spaces"))
        if "authToken=" not in prepared.headers.get("Cookie", ""):
            raise PublisherError("Session cookie does not apply to the configured URL; check HTTPS, cookie domain/path, or log in again")

    def logout(self) -> None:
        self.post("auth/logout")
        self.session.cookies.clear()
        self.logged_in = False

    def close(self) -> None:
        # Do NOT log out here. A cached session is deliberately reusable.
        self.session.cookies.clear()
        self.session.close()
        self.logged_in = False

    def all_items(self, endpoint: str, payload: dict) -> list[dict]:
        items, seen_ids, seen_cursors = [], set(), set()
        cursor = None
        for _ in range(10000):
            request = {**payload, "limit": 100}
            if cursor:
                request["cursor"] = cursor
            body = self.post(endpoint, request, read_only=True)
            if not isinstance(body, dict) or not isinstance(body.get("items"), list) or not isinstance(body.get("meta"), dict):
                raise PublisherError(f"Unknown pagination format on {endpoint}; refusing incomplete enumeration")
            meta = body["meta"]
            if type(meta.get("hasNextPage")) is not bool:
                raise PublisherError(f"Missing boolean meta.hasNextPage on {endpoint}")
            for row in body["items"]:
                row = require_record(row, endpoint)
                if row["id"] in seen_ids:
                    raise PublisherError(f"Repeated record while listing {endpoint}; remote tree may be changing")
                seen_ids.add(row["id"])
                items.append(row)
            # An empty batch may still have a next cursor after permission filtering.
            if not meta["hasNextPage"]:
                return items
            cursor = meta.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise PublisherError(f"Invalid/repeated pagination cursor on {endpoint}")
            seen_cursors.add(cursor)
        raise PublisherError(f"Excessive pagination on {endpoint}")

    def spaces(self) -> list[dict]:
        return self.all_items("spaces", {})

    def info(self, page_id: str) -> dict:
        return require_record(self.post("pages/info", {"pageId": page_id, "format": "json"}, read_only=True), "page info")

    def children(self, space_id: str, parent: str | None) -> list[dict]:
        body = {"pageId": parent} if parent else {"spaceId": space_id}
        result = self.all_items("pages/sidebar-pages", body)
        for row in result:
            if "parentPageId" not in row or row.get("parentPageId") != parent or row.get("spaceId") != space_id or row.get("deletedAt"):
                raise PublisherError("Sidebar response violates requested parent/space scope; refusing to continue")
        return result

    def create(self, space_id: str, parent: str | None, title: str) -> dict:
        payload = {"spaceId": space_id, "title": title, "content": EMPTY_DOC, "format": "json"}
        if parent:
            payload["parentPageId"] = parent
        return require_record(self.post("pages/create", payload), "page create")

    def replace(self, page_id: str, title: str, markdown: str) -> None:
        content, fmt = (markdown, "markdown") if markdown.strip() else (EMPTY_DOC, "json")
        self.post("pages/update", {"pageId": page_id, "title": title, "operation": "replace", "format": fmt, "content": content})

    def trash(self, page_id: str) -> None:
        self.post("pages/delete", {"pageId": page_id, "permanentlyDelete": False})

    def upload(self, page_id: str, asset: Asset) -> str:
        mime = mimetypes.guess_type(asset.path.name)[0] or "application/octet-stream"
        with asset.snapshot.open("rb") as stream:
            # Form fields precede the file part, as required by Fastify multipart.
            result = self.post("files/upload", files={"file": (asset.path.name, stream, mime)}, data={"pageId": page_id})
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise PublisherError("Upload succeeded without a usable URL; not retrying an ambiguous upload")
        url = urljoin(self.base_url + "/", result["url"])
        if urlsplit(url).scheme not in {"https", "http"}:
            raise PublisherError("Upload returned a non-HTTP(S) URL")
        return url


def validate_page(page: dict, space_id: str, parent: str | None = None, *, check_parent: bool = False) -> None:
    require_record(page, "page")
    if page.get("spaceId") != space_id or page.get("deletedAt"):
        raise PublisherError("Destination page belongs to another space or is deleted")
    if check_parent and ("parentPageId" not in page or page["parentPageId"] != parent):
        raise PublisherError("Destination parent changed; refusing to write outside the planned tree")
    if page.get("isBase"):
        raise PublisherError("Bases/database pages cannot be managed by this Markdown publisher")
    if page.get("isLocked"):
        raise PublisherError("A managed page is locked; unlock it explicitly before publishing")
    permissions = page.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise PublisherError("Unknown page permissions response; refusing publication")
    if permissions.get("canEdit") is False or page.get("canEdit") is False:
        raise PublisherError("Publishing account cannot edit a managed page")


def select_space(client: DocmostClient, config: dict) -> dict:
    spaces = client.spaces()
    wanted = config["space"]
    if config["space_id"]:
        matches = [s for s in spaces if s["id"] == config["space_id"]]
    else:
        # Exact name wins; otherwise accept the space slug (e.g. "geomar" for "Geomar").
        matches = [s for s in spaces if s.get("name") == wanted] or [s for s in spaces if s.get("slug") == wanted]
    if len(matches) != 1:
        available = ", ".join(f"{s.get('name')!r} (slug {s.get('slug')!r})" for s in spaces) or "none"
        raise PublisherError(f"Expected one accessible matching space, found {len(matches)}. "
                             f"Available: {available}. Use docmost-publish spaces and pin space_id.")
    space = matches[0]
    if config["space_id"] and wanted and wanted not in {space.get("name"), space.get("slug")}:
        raise PublisherError("Configured space_id and space name disagree")
    if not isinstance(space.get("slug"), str) or not space["slug"]:
        raise PublisherError("Space response lacks slug; cannot construct Docmost page URLs")
    return space


def select_root(client: DocmostClient, space: dict, config: dict) -> dict | None:
    if config["id"]:
        root = client.info(config["id"])
        validate_page(root, space["id"])
        return root
    candidates = [p for p in client.children(space["id"], None) if p.get("title") == config["title"]]
    if len(candidates) > 1:
        raise PublisherError("Multiple top-level pages have the configured title; set docmost.root.id")
    if not candidates:
        return None
    root = client.info(candidates[0]["id"])
    validate_page(root, space["id"], None, check_parent=True)
    return root


def snapshot_descendants(client: DocmostClient, space_id: str, root_id: str, maximum: int) -> list[dict]:
    stack, result, seen = [root_id], [], {root_id}
    while stack:
        parent = stack.pop()
        for page in client.children(space_id, parent):
            validate_page(page, space_id, parent, check_parent=True)
            if page["id"] in seen:
                raise PublisherError("Cycle or duplicate page in remote hierarchy")
            seen.add(page["id"])
            result.append(page)
            if len(result) > maximum:
                raise PublisherError(f"Remote descendant count exceeds max_delete_pages={maximum}; nothing deleted")
            # Do not rely on hasChildren; enumerate every managed node.
            stack.append(page["id"])
    return result


def page_url(base: str, space: dict, page: dict) -> str:
    slug_id = page.get("slugId")
    if not isinstance(slug_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", slug_id):
        raise PublisherError("Page response lacks a valid slugId; cannot build page URL")
    title = unicodedata.normalize("NFKD", page.get("title") or "untitled").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()[:70]).strip("-") or "page"
    return f"{base}/s/{quote(space['slug'], safe='')}/p/{slug}-{quote(slug_id, safe='')}"


def has_body(content: Any) -> bool:
    if not isinstance(content, dict):
        return False
    if content.get("text", "").strip():
        return True
    if content.get("type") not in {"doc", "paragraph", "text"}:
        return True
    return any(has_body(c) for c in content.get("content", []))


def document_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    return str(content.get("text", "")) + "".join(document_text(c) for c in content.get("content", []))


def verify_page(client: DocmostClient, page: dict, space_id: str, parent: str | None,
                title: str, nonempty: bool, attempts: int, required_text: str | None = None) -> None:
    for attempt in range(attempts):
        current = client.info(page["id"])
        validate_page(current, space_id, parent, check_parent=True)
        content = current.get("content")
        matches = (current.get("title") == title and isinstance(content, dict) and content.get("type") == "doc"
                   and has_body(content) == nonempty)
        if matches and (required_text is None or required_text in document_text(content)):
            return
        if attempt + 1 < attempts:
            time.sleep(0.5)
    raise PublisherError(f"Read-back verification failed for page {page['id']}; tree may be partially published")


class Journal:
    """Per-run diagnostic journal, NOT synchronization state or a complete backup."""
    def __init__(self, directory: Path, initial: dict):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = directory / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
        self.data = {"started_at": stamp, "status": "running", **initial, "operations": []}
        self.save()

    def save(self) -> None:
        fd, name = tempfile.mkstemp(prefix=".journal-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def run(self, name: str, details: dict, action: Any) -> Any:
        entry = {"operation": name, **details, "status": "pending"}
        self.data["operations"].append(entry)
        self.save()  # An interrupted/ambiguous request remains marked pending.
        result = action()
        if isinstance(result, dict) and "id" in result:
            entry["result_id"] = result["id"]
        entry["status"] = "done"
        self.save()
        return result


@contextmanager
def local_lock(directory: Path, target: str) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(target.encode()).hexdigest() + ".lock"
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublisherError("Another publisher using this local lock directory is active for this space") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def print_plan(tree: SourceTree, space: dict | None = None, root: dict | None = None,
               old: list[dict] | None = None) -> None:
    print(f"Source: {tree.root}")
    if space:
        print(f"Space: {space.get('name')} [{space['id']}]")
        print(f"Root: {tree.root_title} [{root['id'] if root else 'will be created'}]")
        print(f"Existing descendants to trash: {len(old or [])}")
    print(f"Local Markdown files: {tree.markdown_count}; resulting pages including root: {len(tree.nodes)}; unique local assets: {len(tree.assets)}")
    for node in tree.ordered():
        print(f"  {node.key!r} => {node.title!r} (parent: {node.parent!r})")
    for warning in sorted(tree.warnings):
        print(f"WARNING: {warning}")


def preflight_probe(client: DocmostClient, space: dict, root: dict, cfg: dict, journal: Journal) -> None:
    """Test create, Markdown replacement, read-back, JSON clearing, and trash first."""
    title = "Publisher compatibility check " + uuid.uuid4().hex[:8]
    probe = journal.run("probe_create", {"parent": root["id"]}, lambda: client.create(space["id"], root["id"], title))
    validate_page(probe, space["id"], root["id"], check_parent=True)
    probe_child = journal.run("probe_create_child", {"parent": probe["id"]},
                              lambda: client.create(space["id"], probe["id"], "Nested compatibility check"))
    validate_page(probe_child, space["id"], probe["id"], check_parent=True)
    sample = "Docmost publisher compatibility check."
    journal.run("probe_markdown", {"id": probe["id"]}, lambda: client.replace(probe["id"], title, sample))
    verify_page(client, probe, space["id"], root["id"], title, True, cfg["verify_attempts"], sample)
    journal.run("probe_clear", {"id": probe["id"]}, lambda: client.replace(probe["id"], title, ""))
    verify_page(client, probe, space["id"], root["id"], title, False, cfg["verify_attempts"])
    journal.run("probe_trash", {"id": probe["id"]}, lambda: client.trash(probe["id"]))
    if any(p["id"] == probe["id"] for p in client.children(space["id"], root["id"])):
        raise PublisherError("Compatibility-check page remained visible after trashing; original tree has not been deleted")
    try:
        child_after = client.info(probe_child["id"])
    except APIError as exc:
        if exc.status != 404:
            raise PublisherError("Cannot verify recursive trash behavior; original tree has not been deleted") from exc
    else:
        if not child_after.get("deletedAt"):
            raise PublisherError("Deleting a page did not trash its child; original tree has not been deleted")


def rebuild(client: DocmostClient, tree: SourceTree, space: dict, root: dict | None,
            old: list[dict], cfg: dict, journal: Journal) -> dict:
    pub = cfg["publish"]
    try:
        if root is None:
            root = journal.run("create_root", {"title": tree.root_title},
                               lambda: client.create(space["id"], None, tree.root_title))
            validate_page(root, space["id"], None, check_parent=True)
        journal.data["root_id"] = root["id"]
        journal.save()
        root_parent = root.get("parentPageId")
        if pub["preflight_write_probe"]:
            preflight_probe(client, space, root, pub, journal)
        current = snapshot_descendants(client, space["id"], root["id"], pub["max_delete_pages"])
        signature = lambda rows: {(p["id"], p["parentPageId"]) for p in rows}
        if signature(current) != signature(old):
            raise PublisherError("Remote hierarchy changed since the plan; refusing replacement")
        validate_page(client.info(root["id"]), space["id"], root_parent, check_parent=True)
        old_children = [p for p in old if p["parentPageId"] == root["id"]]
        for child in old_children:
            if child["id"] == root["id"]:
                raise PublisherError("Refusing to delete retained root")
            validate_page(client.info(child["id"]), space["id"], root["id"], check_parent=True)
            LOG.info("Trashing old subtree: %s [%s]", child.get("title"), child["id"])
            journal.run("trash_old_subtree", {"id": child["id"], "parent": root["id"]}, lambda c=child: client.trash(c["id"]))
        if client.children(space["id"], root["id"]):
            raise PublisherError("Root still has children after deletion; aborting instead of mixing trees")
        # Remove stale root navigation before constructing its new targets.
        journal.run("clear_root", {"id": root["id"]}, lambda: client.replace(root["id"], tree.root_title, ""))
        root["title"] = tree.root_title
        pages = {".": root}
        for node in tree.ordered():
            if node.key == ".":
                continue
            parent_id = pages[node.parent]["id"]
            LOG.info("Creating: %s", node.key)
            page = journal.run("create_page", {"source": node.key, "parent": parent_id},
                               lambda n=node, p=parent_id: client.create(space["id"], p, n.title))
            validate_page(page, space["id"], parent_id, check_parent=True)
            pages[node.key] = page
        urls = {key: page_url(client.base_url, space, page) for key, page in pages.items()}
        uploads: dict[tuple[str, str], str] = {}
        def upload(key: str, asset: Asset) -> str:
            cache_key = (pages[key]["id"], asset.digest)
            if cache_key not in uploads:
                LOG.info("Uploading asset for %s: %s", key, asset.path.name)
                uploads[cache_key] = journal.run("upload_asset", {"page_id": pages[key]["id"], "file": asset.path.name, "sha256": asset.digest},
                                                lambda: client.upload(pages[key]["id"], asset))
            return uploads[cache_key]
        # Uploads and rendering finish before page body writes; all IDs are known.
        rendered = {n.key: tree.render(n, urls, upload) for n in tree.ordered()}
        nodes = list(tree.ordered())
        # Publish root last, so its new navigation appears after child bodies exist.
        for node in nodes[1:] + nodes[:1]:
            page = pages[node.key]
            body = rendered[node.key]
            LOG.info("Publishing body: %s", node.key)
            journal.run("replace_body", {"id": page["id"], "source": node.key},
                        lambda p=page, n=node, b=body: client.replace(p["id"], n.title, b))
            if pub["verify"]:
                parent_id = root_parent if node.key == "." else pages[node.parent]["id"]
                # Verifies metadata and nonempty/empty structured content, not a
                # byte-for-byte Markdown round trip or rendering of every feature.
                nonempty = bool(body.strip())
                if node.source_file and node.tokens:
                    nonempty = any(t["type"] != "blank_line" for t in node.tokens)
                verify_page(client, page, space["id"], parent_id, node.title, nonempty, pub["verify_attempts"])
        final_rows = snapshot_descendants(client, space["id"], root["id"], pub["max_pages"])
        expected = {(p["id"], pages[n.parent]["id"]) for n in nodes[1:] for p in [pages[n.key]]}
        if signature(final_rows) != expected:
            raise PublisherError("Final hierarchy differs from the local tree; another publisher/editor may be active")
        journal.data.update({"status": "completed", "root_url": urls["."], "page_map": {k: {"id": p["id"], "url": urls[k]} for k, p in pages.items()},
                             "uploads": len(uploads), "completed_at": datetime.now(timezone.utc).isoformat()})
        journal.save()
        return {"root_id": root["id"], "root_url": urls["."], "pages": len(pages), "uploads": len(uploads)}
    except BaseException as exc:
        journal.data["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        journal.data["failure_type"] = type(exc).__name__
        journal.save()
        raise


"""Local Markdown snapshot, validation, and page-tree rendering.

Source files are never modified. Markdown is parsed and re-serialized, so the
published source is normalized; fenced code and inline-code values are retained.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import html
import os
import re
import stat
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, unquote, urlsplit

import mistune
import yaml
from mistune.renderers.markdown import MarkdownRenderer


class PublisherError(Exception):
    """A validation, compatibility, or publication failure."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently using the last value."""


def _unique_mapping(loader: UniqueKeyLoader, node: Any, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise PublisherError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise PublisherError(f"Duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        # Do not echo arbitrary config contents (which could include secrets).
        raise PublisherError("Invalid YAML; check indentation and scalar types") from exc


def walk_tokens(tokens: list[dict]) -> Iterator[dict]:
    for token in tokens:
        yield token
        yield from walk_tokens(token.get("children", []))


def clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def escape_text(value: str) -> str:
    # Escape literal text only, never actual Markdown syntax or code tokens.
    value = html.unescape(value).replace("&", "&amp;")
    return re.sub(r"([\\`*_\[\]<>#!|~+.$-])", r"\\\1", value)


class NormalizedMarkdownRenderer(MarkdownRenderer):
    """CommonMark plus tables, task lists, strike-through, footnotes, and math."""

    def render_referrences(self, state: Any) -> list:
        # Reference links are expanded to inline links after URL rewriting.
        return []

    def text(self, token: dict, state: Any) -> str:
        return escape_text(token["raw"])

    def link(self, token: dict, state: Any) -> str:
        text = self.render_children(token, state)
        url = token["attrs"]["url"]
        url = quote(url, safe="/:?#[]@!$&'()*+,;=%~_-.")
        title = token["attrs"].get("title")
        suffix = ""
        if title is not None:
            title = str(title).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            suffix = f' "{title}"'
        return f"[{text}](<{url}>{suffix})"

    def codespan(self, token: dict, state: Any) -> str:
        text = token["raw"]
        longest = max((len(s) for s in re.findall(r"`+", text)), default=0)
        fence = "`" * (longest + 1)
        pad = bool(text) and (text.startswith("`") or text.endswith("`") or
                              (text.startswith(" ") and text.endswith(" ") and text.strip()))
        return fence + (" " if pad else "") + text + (" " if pad else "") + fence

    def block_code(self, token: dict, state: Any) -> str:
        code = token["raw"]
        info = token.get("attrs", {}).get("info", "")
        character = "~" if "`" in info else "`"
        longest = max((len(s) for s in re.findall(re.escape(character) + r"+", code)), default=0)
        fence = character * max(3, longest + 1)
        return f"{fence}{info}\n{code}" + ("" if code.endswith("\n") else "\n") + f"{fence}\n\n"

    def strikethrough(self, token: dict, state: Any) -> str:
        return "~~" + self.render_children(token, state) + "~~"

    def task_marker(self, token: dict, state: Any) -> str:
        return token["raw"]

    def list(self, token: dict, state: Any) -> str:
        token = copy.deepcopy(token)
        for item in token["children"]:
            if item["type"] == "task_list_item":
                marker = "[x] " if item.get("attrs", {}).get("checked") else "[ ] "
                if not item.get("children"):
                    item["children"] = [{"type": "block_text", "children": []}]
                first = item["children"][0]
                if "children" not in first:
                    raise PublisherError("Unsupported task-list structure")
                first["children"].insert(0, {"type": "task_marker", "raw": marker})
        return super().list(token, state)

    def table(self, token: dict, state: Any) -> str:
        head, body = token["children"]
        def row(cells: list[dict]) -> str:
            values = []
            for cell in cells:
                value = self.render_children(cell, state).replace("\n", " ")
                # Pipes inside code spans also need protection inside GFM tables.
                value = re.sub(r"(?<!\\)\|", r"\\|", value)
                values.append(value)
            return "| " + " | ".join(values) + " |\n"
        alignments = {"left": ":---", "right": "---:", "center": ":---:"}
        separator = "| " + " | ".join(
            alignments.get(c.get("attrs", {}).get("align"), "---") for c in head["children"]
        ) + " |\n"
        return row(head["children"]) + separator + "".join(
            row(r["children"]) for r in body["children"]
        ) + "\n"

    def footnote_ref(self, token: dict, state: Any) -> str:
        return f"[^{token['attrs']['index']}]"

    def footnotes(self, token: dict, state: Any) -> str:
        return self.render_children(token, state)

    def footnote_item(self, token: dict, state: Any) -> str:
        body = self.render_children(token, state).rstrip().splitlines()
        prefix = f"[^{token['attrs']['index']}]: "
        return prefix + (body[0] if body else "") + "\n" + "".join(
            "    " + line + "\n" for line in body[1:]
        ) + "\n"

    def inline_math(self, token: dict, state: Any) -> str:
        return "$" + token["raw"] + "$"

    def block_math(self, token: dict, state: Any) -> str:
        return "$$\n" + token["raw"] + "\n$$\n\n"


class HTMLReferenceChecker(HTMLParser):
    """Raw HTML is passed through, but local resources in HTML are rejected."""
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if value is None:
                continue
            if key in {"srcset", "style"} and (key == "srcset" or "url(" in value.lower()):
                raise PublisherError("HTML srcset / CSS url() is unsupported; use Markdown image syntax")
            if key in {"href", "src", "poster", "data", "xlink:href"}:
                parsed = urlsplit(value)
                if not parsed.scheme and not parsed.netloc and parsed.path:
                    raise PublisherError(f"Local HTML reference {value!r}; use a Markdown link/image instead")

    handle_startendtag = handle_starttag


@dataclass
class PageSource:
    key: str                         # '.' for root; otherwise source-relative POSIX path
    parent: str | None
    title: str
    directory: bool
    source_file: Path | None
    tokens: list[dict] = field(default_factory=list)
    state: Any = None
    children: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Target:
    kind: str                        # external, page, asset
    value: str
    suffix: str = ""


@dataclass(frozen=True)
class Asset:
    path: Path
    snapshot: Path
    digest: str
    size: int


class SourceTree:
    def __init__(self, root: Path, config_path: Path, root_title: str,
                 source: dict, publish: dict, links: dict, assets: dict, scratch: Path):
        self.root = root
        self.config_path = config_path
        self.root_title = root_title
        self.source = source
        self.publish = publish
        self.links = links
        self.asset_config = assets
        self.scratch = scratch
        self.nodes: dict[str, PageSource] = {}
        self.aliases: dict[str, str] = {}
        self.targets: dict[tuple[str, str, bool], Target] = {}
        self.assets: dict[str, Asset] = {}
        self.warnings: set[str] = set()
        self.markdown_count = 0
        self.total_bytes = 0
        self.parser = mistune.create_markdown(
            renderer="ast", plugins=["table", "task_lists", "strikethrough", "footnotes", "math"]
        )

    def ignored(self, relative: str) -> bool:
        parts = Path(relative).parts
        if any(part.startswith(".") for part in parts):
            return True
        if self.root / relative == self.config_path:
            return True
        ancestors = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        for pattern in self.source["exclude"]:
            options = [pattern]
            if pattern.startswith("**/"):
                options.append(pattern[3:])
            for option in options:
                for path in ancestors:
                    if fnmatch.fnmatchcase(path, option) or fnmatch.fnmatchcase(path + "/", option):
                        return True
        return False

    def _safe_path(self, path: Path) -> Path:
        # Normalize '..' lexically, then explicitly reject every symlink component.
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(self.root):
            raise PublisherError(f"Path escapes source directory: {path}")
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise PublisherError(f"Symlink not allowed in publishable paths: {current}")
        return path

    def _read(self, path: Path, limit: int) -> bytes:
        path = self._safe_path(path)
        # O_NONBLOCK prevents hanging on an unexpected FIFO. O_NOFOLLOW protects
        # the final component; use an immutable checkout to avoid concurrent edits.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PublisherError(f"Not a regular file: {path}")
            if before.st_size > limit:
                raise PublisherError(f"File exceeds configured size limit: {path}")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(data) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise PublisherError(f"File changed while reading, or exceeds limit: {path}")
        self.total_bytes += len(data)
        if self.total_bytes > self.source["max_total_bytes"]:
            raise PublisherError("Source snapshot exceeds source.max_total_bytes")
        return data

    def _document(self, path: Path) -> tuple[list[dict], Any]:
        try:
            text = self._read(path, self.source["max_markdown_bytes"]).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PublisherError(f"Markdown is not UTF-8: {path}") from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines(keepends=True)
        if lines and lines[0].strip() == "---":
            # Front matter is not published; it plays no part in page titles.
            end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
            if end is not None:
                text = "".join(lines[end + 1:])
        tokens, state = self.parser.parse(text)
        self.markdown_count += 1
        return tokens, state

    def build(self) -> "SourceTree":
        if not self.root.is_dir() or self.root == Path("/"):
            raise PublisherError("Source must be an existing documentation directory, not filesystem root")

        def visit(directory: Path, parent: str | None, depth: int) -> str | None:
            if depth > self.publish["max_depth"]:
                raise PublisherError("Directory depth exceeds publish.max_depth")
            relative = directory.relative_to(self.root).as_posix()
            entries = sorted(directory.iterdir(), key=lambda p: (p.name.casefold(), p.name))
            files, dirs = [], []
            for entry in entries:
                rel = entry.relative_to(self.root).as_posix()
                if self.ignored(rel):
                    continue
                if entry.is_symlink():
                    raise PublisherError(f"Symlink in source; remove or exclude it: {entry}")
                if entry.is_dir():
                    dirs.append(entry)
                elif entry.suffix.lower() in {".md", ".markdown"}:
                    files.append(entry)
            indexes = [p for p in files if p.name.casefold() == self.source["index_file"].casefold()]
            if len(indexes) > 1:
                raise PublisherError(f"Multiple directory index files: {directory}")
            index = indexes[0] if indexes else None
            tokens, state = self._document(index) if index else ([], None)
            # Page titles are the exact file/directory names; content never renames a page.
            node = PageSource(relative, parent, self.root_title if parent is None else directory.name,
                              True, index, tokens, state)
            self.nodes[relative] = node
            self.aliases[relative] = relative
            if index:
                self.aliases[index.relative_to(self.root).as_posix()] = relative
            for subdir in dirs:
                child = visit(subdir, relative, depth + 1)
                if child:
                    node.children.append(child)
            for path in files:
                if path == index:
                    continue
                key = path.relative_to(self.root).as_posix()
                body, file_state = self._document(path)
                self.nodes[key] = PageSource(key, relative, path.stem, False, path, body, file_state)
                self.aliases[key] = key
                node.children.append(key)
            if parent is not None and not index and not node.children:
                del self.nodes[relative]
                del self.aliases[relative]
                return None
            if len(self.nodes) > self.publish["max_pages"]:
                raise PublisherError("Page count exceeds publish.max_pages")
            return relative

        visit(self.root, None, 0)
        if self.markdown_count == 0 and not self.publish["allow_empty"]:
            raise PublisherError("No Markdown files found; refusing replacement (allow_empty is false)")
        for node in self.nodes.values():
            for token in walk_tokens(node.tokens):
                if token["type"] in {"link", "image"}:
                    url = token["attrs"]["url"]
                    key = (node.key, url, token["type"] == "image")
                    # Unresolvable links never block publication: nothing is read
                    # or uploaded for them, and the original URL is kept as-is.
                    total_bytes = self.total_bytes
                    try:
                        self.targets[key] = self._resolve(node, url, key[2])
                    except PublisherError as exc:
                        self.total_bytes = total_bytes
                        self.warnings.add(f"{exc} (left unchanged)")
                        self.targets[key] = Target("external", url)
                elif token["type"] in {"inline_html", "block_html"}:
                    try:
                        HTMLReferenceChecker().feed(token["raw"])
                    except PublisherError as exc:
                        self.warnings.add(f"{exc} in {node.key} (left unchanged)")
        # Force every renderer path to run before any remote destructive operation.
        preview_urls = {key: f"https://preview.invalid/p/{quote(key, safe='')}" for key in self.nodes}
        for node in self.nodes.values():
            self.render(node, preview_urls, lambda key, asset: "https://preview.invalid/asset/" + asset.digest)
        return self

    def _resolve(self, node: PageSource, url: str, image: bool) -> Target:
        if any(ord(c) < 32 for c in url):
            raise PublisherError(f"Control character in a URL in {node.key}")
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme and parsed.scheme.lower() not in {"http", "https", "mailto", "tel"}:
                raise PublisherError(f"Unsupported URL scheme {parsed.scheme!r} in {node.key}")
            return Target("external", url)
        if not parsed.path:
            if parsed.fragment:
                self.warnings.add("Heading fragments are retained but are not translated to Docmost heading IDs.")
            return Target("external", url if self.links["fragment_policy"] == "preserve" else ("?" + parsed.query if parsed.query else ""))
        path_text = unquote(parsed.path)
        if "\x00" in path_text or "\\" in path_text:
            raise PublisherError(f"Invalid local URL in {node.key}: {url!r}")
        base = node.source_file.parent if node.source_file else self.root / node.key
        path = self._safe_path(self.root / path_text.lstrip("/") if path_text.startswith("/") else base / path_text)
        relative = path.relative_to(self.root).as_posix()
        if relative != "." and self.ignored(relative):
            raise PublisherError(f"Link targets an excluded/hidden file in {node.key}: {url}")
        suffix = ("?" + parsed.query) if parsed.query else ""
        if parsed.fragment and self.links["fragment_policy"] == "preserve":
            suffix += "#" + parsed.fragment
        if relative in self.aliases:
            if image:
                raise PublisherError(f"Image points to a document/directory in {node.key}: {url}")
            if parsed.fragment:
                self.warnings.add("Heading fragments are retained/dropped per configuration, not translated to Docmost heading IDs.")
            return Target("page", self.aliases[relative], suffix)
        if not path.is_file() or path.suffix.lower() in {".md", ".markdown"}:
            raise PublisherError(f"Missing/unpublished local target in {node.key}: {url}")
        if not self.asset_config["enabled"]:
            raise PublisherError(f"Local attachment found but assets.enabled is false: {url}")
        if path.suffix.lower() not in self.asset_config["allowed_extensions"]:
            raise PublisherError(f"Attachment extension is not allowlisted: {relative}")
        if relative not in self.assets:
            data = self._read(path, self.asset_config["max_bytes"])
            digest = hashlib.sha256(data).hexdigest()
            snapshot = self.scratch / digest
            if not snapshot.exists():
                snapshot.write_bytes(data)
            self.assets[relative] = Asset(path, snapshot, digest, len(data))
        return Target("asset", relative, suffix)

    def ordered(self) -> Iterator[PageSource]:
        def visit(key: str) -> Iterator[PageSource]:
            node = self.nodes[key]
            yield node
            for child in node.children:
                yield from visit(child)
        return visit(".")

    def render(self, node: PageSource, page_urls: dict[str, str],
               upload: Callable[[str, Asset], str]) -> str:
        if node.source_file is None:
            if not node.children:
                return ""
            lines = ["# " + escape_text(node.title), "", "## Contents", ""]
            lines.extend(f"- [{escape_text(self.nodes[c].title)}](<{page_urls[c]}>)" for c in node.children)
            return "\n".join(lines) + "\n"
        tokens = copy.deepcopy(node.tokens)
        for token in walk_tokens(tokens):
            if token["type"] in {"link", "image"}:
                url = token["attrs"]["url"]
                target = self.targets[(node.key, url, token["type"] == "image")]
                if target.kind == "page":
                    rewritten = page_urls[target.value] + target.suffix
                elif target.kind == "asset":
                    rewritten = upload(node.key, self.assets[target.value]) + target.suffix
                else:
                    rewritten = target.value
                token["attrs"]["url"] = rewritten
                token.pop("label", None)
                token.pop("ref", None)
        return NormalizedMarkdownRenderer()(tokens, node.state)

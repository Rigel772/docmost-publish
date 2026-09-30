"""Installable Linux CLI. With no subcommand, publish the current folder."""
from __future__ import annotations

import argparse
import copy
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml

from . import __version__
from .auth import SessionStore, authenticate, describe_session
from .engine import (
    APIError, DEFAULTS, DocmostClient, Journal, local_lock, print_plan,
    read_config, rebuild, select_root, select_space, snapshot_descendants,
    validate_config,
)
from .markdown_tree import PublisherError, SourceTree
from .storage import config_directory, private_directory, state_directory

LOG = logging.getLogger("docmost-publisher")


def find_config(explicit: Path | None, *, required: bool) -> Path | None:
    if explicit is not None:
        path = explicit.expanduser().absolute()
        if path.is_symlink():
            raise PublisherError("The project config must not be a symlink")
        if not path.is_file():
            raise PublisherError(f"Config file not found: {path}")
        return path.resolve()
    matches = [Path.cwd() / name for name in (".docmost.yaml", ".docmost.yml")
               if (Path.cwd() / name).exists() or (Path.cwd() / name).is_symlink()]
    if len(matches) > 1:
        raise PublisherError("Both .docmost.yaml and .docmost.yml exist; keep one or use --config")
    if matches:
        return find_config(matches[0], required=True)
    if required:
        raise PublisherError("No .docmost.yaml in the current folder. Run docmost-publish init, or use --config PATH. Nothing was sent.")
    return None


def write_initial_config(args: argparse.Namespace) -> int:
    path = (args.config or Path.cwd() / ".docmost.yaml").expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise PublisherError(f"Config already exists; refusing to overwrite: {path}")
    interactive = sys.stdin.isatty() and not args.no_input
    def required(value: str | None, label: str, default: str = "") -> str:
        if value:
            return value
        if not interactive:
            if default:
                return default
            raise PublisherError(f"Provide {label} for noninteractive init")
        prompt = label + (f" [{default}]" if default else "") + ": "
        return input(prompt).strip() or default
    url = required(args.url, "--url (Docmost instance URL)")
    space = args.space or ("" if args.space_id else required(None, "--space (exact space name)"))
    title = required(args.root_title, "--root-title", path.parent.name)
    auth = {"method": "session", "profile": args.profile or "default"}
    if args.email:
        auth["email"] = args.email
    dm = {"url": url, "auth": auth, "root": {"title": title}}
    if space:
        dm["space"] = space
    if args.space_id:
        dm["space_id"] = args.space_id
    if args.root_id:
        dm["root"]["id"] = args.root_id
    if args.allow_http:
        dm["http"] = {"allow_http": True}
    data = {
        "version": 1,
        "docmost": dm,
        "source": {"path": ".", "index_file": "README.md", "exclude": ["**/node_modules/**", "**/vendor/**"]},
        "publish": {"mode": "replace_tree", "preserve_root": True, "deletion": "trash", "allow_empty": False},
    }
    # Validate before creating the user's file; the temporary file contains no secret.
    from .engine import merge_config
    validate_config(merge_config(DEFAULTS, data), path)
    text = (
        "# This entire remote subtree is owned by the local Markdown folder.\n"
        "# Passwords and session tokens do not belong in this file.\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    print(f"Created {path}\nNext: docmost-publish login\nThen: docmost-publish --dry-run\nPublish: docmost-publish")
    return 0


def load_for_command(args: argparse.Namespace, command: str) -> tuple[dict, Path | None]:
    publishing = command in {"publish", "check", "plan"}
    path = find_config(args.config, required=publishing)
    if path is not None:
        cfg = read_config(path, require_destination=publishing)
    else:
        cfg = copy.deepcopy(DEFAULTS)
    if args.url:
        cfg["docmost"]["url"] = args.url
    if args.profile:
        cfg["docmost"]["auth"]["profile"] = args.profile
    if args.allow_http:
        cfg["docmost"]["http"]["allow_http"] = True
    if args.email:
        cfg["docmost"]["auth"]["email"] = args.email
    cfg = validate_config(cfg, path or Path.cwd() / ".docmost.yaml", require_destination=publishing)
    return cfg, path


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docmost-publish",
        description="Replace one Docmost page tree from Markdown in the current folder. Default command: publish.",
        epilog="No parent-directory config search. Passwords are never saved; session cookies are cached under your home directory.",
    )
    parser.add_argument("command", nargs="?", default="publish",
                        choices=["publish", "init", "login", "logout", "status", "spaces", "check", "plan"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path, help="Project config path; defaults to .docmost.yaml in the current folder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect local and remote trees without page/attachment writes")
    mode.add_argument("--check", action="store_true", help="Validate local files only; no network activity")
    parser.add_argument("-y", "--yes", action="store_true", help="Acknowledge subtree replacement without typing the root title")
    parser.add_argument("--no-input", action="store_true", help="Never prompt (publication also requires --yes)")
    parser.add_argument("--state-dir", type=Path, help="Override the directory for owner-only run journals and local locks")
    parser.add_argument("--url", help="Instance URL for init/login/logout/status/spaces; publication always uses its config")
    parser.add_argument("--email", help="Account email for init/login (password is prompted or read from its environment variable)")
    parser.add_argument("--profile", help="Session profile for init/login/logout/status/spaces; default: default")
    parser.add_argument("--allow-http", action="store_true", help="Explicitly allow unencrypted HTTP for init/login/logout/status/spaces")
    parser.add_argument("--space", help="Exact existing space name for init")
    parser.add_argument("--space-id", help="Existing space UUID for init")
    parser.add_argument("--root-title", help="Root page title for init")
    parser.add_argument("--root-id", help="Optional existing root page UUID for init")
    parser.add_argument("--verbose", action="store_true", help="Show additional application logging (never HTTP payloads or tokens)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    command = args.command
    if args.dry_run or args.check:
        if command != "publish":
            parser.error("--dry-run and --check are modifiers for the default publish command")
        command = "plan" if args.dry_run else "check"
    if args.yes and command != "publish":
        parser.error("--yes is only valid for publication")
    if command != "init" and any([args.space, args.space_id, args.root_title, args.root_id]):
        parser.error("--space, --space-id, --root-title, and --root-id are init options")
    if command not in {"init", "login"} and args.email:
        parser.error("--email is an init/login option")
    if command in {"publish", "check", "plan"} and any([args.url, args.profile, args.allow_http]):
        parser.error("Publication destination and authentication settings must come from the project config")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    client = None
    try:
        if command == "init":
            return write_initial_config(args)
        cfg, config_path = load_for_command(args, command)
        dm = cfg["docmost"]
        store = SessionStore(dm["url"], dm["auth"]["profile"])
        if command == "status":
            store.prepare()
            print(describe_session(store))
            return 0
        if command in {"login", "logout", "spaces"}:
            client = DocmostClient(dm)
            # Account commands cannot race one another in the same profile on this machine.
            store.prepare()
            with local_lock(store.directory / "locks", dm["url"] + "|" + store.profile):
                if command == "logout":
                    result = 0
                    if store.load(client):
                        try:
                            client.logout()
                        except APIError as exc:
                            if exc.status != 401:
                                LOG.warning("Remote logout failed; the server session may still be active: %s", exc)
                                result = 1
                        except PublisherError as exc:
                            LOG.warning("Remote logout could not be confirmed: %s", exc)
                            result = 1
                        finally:
                            store.remove()
                    else:
                        store.remove()
                    print("Cached token removed. Expired/missing sessions need no local cleanup.")
                    return result
                spaces = authenticate(client, store, dm["auth"], force_login=command == "login",
                                      interactive=not args.no_input, explicit_email=args.email)
                if command == "spaces":
                    for space in spaces:
                        print(f"{space['id']}\t{space.get('slug', '')}\t{space.get('name', '')}")
                else:
                    print(f"Logged in to {dm['url']} (profile {store.profile}).\nSession token: {store.path}\nPassword was not saved.")
                return 0
        assert config_path is not None
        source_path = (config_path.parent / cfg["source"]["path"]).resolve()
        state_dir = (args.state_dir.expanduser().absolute() if args.state_dir else state_directory())
        if state_dir.is_relative_to(source_path) or config_directory().is_relative_to(source_path):
            raise PublisherError("Application token/state directories must be outside the published source tree")
        if command == "publish" and (args.no_input or not sys.stdin.isatty()) and not args.yes:
            raise PublisherError("Noninteractive publication requires --yes. Use --dry-run to inspect without writes.")
        with tempfile.TemporaryDirectory(prefix="docmost-publisher-") as directory:
            tree = SourceTree(source_path, config_path, dm["root"]["title"], cfg["source"], cfg["publish"],
                              cfg["links"], cfg["assets"], Path(directory)).build()
            if command == "check":
                print_plan(tree)
                print("Local validation passed. No network requests were made.")
                return 0
            client = DocmostClient(dm)
            # Hold the profile lock for the run, preventing local logout or token races.
            # This does NOT lock the Docmost server or other machines.
            store.prepare()
            private_directory(state_dir)
            private_directory(state_dir / "locks")
            with local_lock(store.directory / "locks", dm["url"] + "|" + store.profile):
                authenticate(client, store, dm["auth"], interactive=not args.no_input)
                space = select_space(client, dm)
                with local_lock(state_dir / "locks", client.base_url + "|" + space["id"]):
                    root = select_root(client, space, dm["root"])
                    old = snapshot_descendants(client, space["id"], root["id"], cfg["publish"]["max_delete_pages"]) if root else []
                    print_plan(tree, space, root, old)
                    if command == "plan":
                        print("Dry run: no pages or attachments were created, changed, or deleted. Login may cache a session.")
                        return 0
                    if not args.yes:
                        print("ALL descendants, including manually added pages, will be moved to Trash. The root body will be replaced.")
                        if input(f"Type the root title {tree.root_title!r} to continue: ") != tree.root_title:
                            raise PublisherError("Confirmation did not match; no page writes performed")
                    private_directory(state_dir / "runs")
                    journal = Journal(state_dir / "runs", {
                        "instance": client.base_url, "space_id": space["id"], "source": str(source_path),
                        "root_id": root["id"] if root else None,
                        "root_before": copy.deepcopy(root), "old_descendants": old,
                    })
                    print(f"Run journal: {journal.path}", flush=True)
                    result = rebuild(client, tree, space, root, old, cfg, journal)
                    print(f"Published {result['pages']} pages including the root; uploaded {result['uploads']} page-owned assets.")
                    print(f"Root: {result['root_url']}")
                    print(f"Optional destination pin in .docmost.yaml: docmost.root.id: {result['root_id']}")
                    return 0
    except (PublisherError, OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.error("Interrupted. Publication may be partial; inspect the run journal before rerunning.")
        return 130
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())

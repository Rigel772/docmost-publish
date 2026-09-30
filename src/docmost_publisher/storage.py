"""Owner-only application storage. Tokens are plaintext secrets, not encrypted."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .markdown_tree import PublisherError


def app_directory(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    base = Path(value).expanduser() if value else Path.home() / fallback
    if not base.is_absolute():
        raise PublisherError(f"{variable} must be an absolute path")
    return base / "docmost-publisher"


def config_directory() -> Path:
    return app_directory("XDG_CONFIG_HOME", ".config")


def state_directory() -> Path:
    return app_directory("XDG_STATE_HOME", ".local/state")


def private_directory(path: Path) -> Path:
    """Reject symlink components; never chmod unrelated ancestor directories."""
    path = path.absolute()
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        if current.is_symlink():
            raise PublisherError(f"Application storage path must not contain symlinks: {current}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PublisherError(f"Application directory is not owned by the current user: {path}")
    os.chmod(path, 0o700)
    return path


def read_private_json(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PublisherError(f"Token file must be a regular file owned by you: {path}")
        if info.st_mode & 0o077:
            raise PublisherError(f"Token file is accessible to other users; run: chmod 600 {str(path)!r}")
        if info.st_size > 65536:
            raise PublisherError("Token file is unexpectedly large; remove it and log in again")
        try:
            result = json.load(stream)
        except (ValueError, UnicodeError) as exc:
            raise PublisherError("Invalid token file; remove it and log in again") from exc
    if not isinstance(result, dict):
        raise PublisherError("Invalid token file format")
    return result


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    private_directory(path.parent)
    if path.is_symlink():
        raise PublisherError(f"Refusing to overwrite a symlink: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

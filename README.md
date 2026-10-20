# Docmost folder publisher

> **Updating after code changes:** the installed `docmost-publish` is a copy in
> `~/.local/share/docmost-publisher/venv`, not a link to this checkout. Rerun
> `./install.sh` after editing the source; it reinstalls this folder into the same
> environment even when the version number is unchanged. To have edits take effect
> immediately instead, do a one-time editable install:
>
> ```sh
> ~/.local/share/docmost-publisher/venv/bin/pip install -e ~/code/docmost-publish
> ```

A Linux CLI that treats a local Markdown folder as authoritative and replaces one
Docmost page subtree on every publication. There is no synchronization database,
merge logic, or persistent per-file identity mapping. Home-directory files hold
login sessions, local locks, and diagnostic run journals.

**Destructive scope:** the configured root page is retained; its body is replaced
and all its existing descendants are moved to Trash. Manually edited or manually
added pages inside this subtree are not preserved in the new tree. Child IDs
change every run. Use a dedicated, disposable publishing subtree.

The implementation uses a normal Docmost account session (`authToken` cookie),
not an enterprise API key, direct SQL, or a modified Docmost server. These are
version-dependent internal HTTP endpoints. The code has automated unit and local
HTTP-mock tests, but has not been tested against your instance or a live Docmost
server. See [API_CONTRACT.md](API_CONTRACT.md) for the upstream interfaces checked.

## Install once

Python 3.11+ is required. Extract this package and run as your normal Linux user:

```sh
cd docmost-publisher
bash ./install.sh
```

The installer creates an isolated virtual environment under
`~/.local/share/docmost-publisher/venv` (or `$XDG_DATA_HOME/docmost-publisher/venv`)
and installs `~/.local/bin/docmost-publish`. It needs package-index access to
install dependencies. It does not use `sudo`, change system Python packages, or
modify shell startup files. The extracted source can be removed after installation.

For bash/zsh when the command is not already on PATH:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Add that line to the appropriate shell startup file for persistence. In fish:

```fish
fish_add_path ~/.local/bin
```

Verify:

```sh
docmost-publish --help
docmost-publish --version
```

Alternatively, in an environment with `uv` already installed:

```sh
uv tool install .
```

Or install the package in your own Python virtual environment with `python -m pip
install .`. Do not use a system-wide `sudo pip install`.

## Configure each project folder

```sh
cd ~/projects/my-erp/docs

docmost-publish init \
  --url https://wiki.example.com \
  --space Engineering \
  --root-title 'ERP documentation'
```

This creates `.docmost.yaml` in the current directory without overwriting an
existing config. `init` can also prompt interactively. It does not log in or
create anything in Docmost.

A minimal config is:

```yaml
version: 1

docmost:
  url: "https://wiki.example.com"
  space: "Engineering"
  root:
    title: "ERP documentation"
```

A more explicit example, matching the supplied example project:

```yaml
version: 1

docmost:
  url: "https://wiki.example.com"
  auth:
    method: "session"
    profile: "default"
  space: "Engineering"
  # space_id: "existing-space-uuid"
  root:
    title: "ERP documentation"
    # id: "existing-root-page-uuid"

source:
  path: "."
  index_file: "README.md"
  exclude:
    - "**/node_modules/**"
    - "**/vendor/**"
    # - "drafts/**"   # Optional: exclude unpublished drafts explicitly.

publish:
  mode: "replace_tree"
  preserve_root: true
  deletion: "trash"
  allow_empty: false
```

`source.path` is relative to the config file, not the shell working directory.
The default is the directory containing the config. This allows:

```sh
docmost-publish --config ~/projects/my-erp/docs/.docmost.yaml
```

Without `--config`, the CLI looks only in the current directory for `.docmost.yaml`
or `.docmost.yml`. It does not walk up to parent folders. Two matching config files
are ambiguous and rejected. No config means an error and no network requests.

An existing space is required; the publisher does not create spaces. Resolve by
exact, case-sensitive space name or pin `space_id`. When both are set they must
agree. Resolve the root by a unique exact top-level title or pin `root.id`; an
explicit ID can also designate a nested root, provided it is in the correct space.
No matching title creates a root; multiple matches abort. An explicit inaccessible,
deleted, or wrong-space root is never silently recreated.

The command prints the root ID after publishing. Pin that ID in the config to
keep the same destination if you later change its title. Root pinning is optional
and is not per-file synchronization state. Config files are never silently rewritten.

## Log in once and reuse the token

From the configured folder:

```sh
docmost-publish login
```

The CLI prompts for email and password. The password is not saved. You can supply
the email with `--email` or configure `docmost.auth.email`.

Log in without a project config:

```sh
docmost-publish login --url https://wiki.example.com --email publisher@example.com
```

The session cookie is stored at:

```text
~/.config/docmost-publisher/sessions/<sha256-of-instance-and-profile>.json
```

`XDG_CONFIG_HOME` overrides the `~/.config` base. The application and session
directories are set to `0700`; token files are atomically written with `0600`.
Symlinked application-storage paths and insecure token files are rejected. Session
cookies are scoped to the exact configured instance URL and the named profile.
Two project folders using the same instance and profile reuse the same login.
Different instances do not share token files. Use separate profiles for multiple
accounts on one instance:

```yaml
docmost:
  url: "https://wiki.example.com"
  auth:
    profile: "publisher"
  space: "Engineering"
  root:
    title: "ERP documentation"
```

**The token is stored in plaintext protected by filesystem permissions, not
cryptographically encrypted.** It is a credential with the account's access.
Do not commit it, attach it to tickets, or include it in unprotected backups.
The CLI never prints the token. `status` prints only local metadata:

```sh
docmost-publish status
docmost-publish logout
```

`status` does not contact the server and does not prove session validity. At the
start of publication, a read request validates the cached session. Expiration or
an HTTP 401 triggers a new interactive login (or requires environment credentials
in noninteractive mode). A network failure or HTTP 403 does not trigger login.
Once destructive publication has started, authentication errors stop the run;
the CLI does not reauthenticate and retry writes behind your back.

Normal publication closes its HTTP connection **without logging out**, so the
cached token remains reusable. `login` explicitly replaces the profile's login;
`logout` attempts to revoke the session and removes the local token. A failed
remote logout is reported and may require revocation through Docmost's UI.

MFA and enforced SSO flows are not implemented or bypassed. Use an account and
instance that permit ordinary password login. The account must be authorized to
read, create, edit, and trash pages throughout the dedicated subtree. Avoid
restricted/hidden child pages and concurrent manual editing in that subtree.

## Publish from any configured folder

```sh
cd ~/projects/my-erp/docs

docmost-publish --check       # local validation; no network
docmost-publish --dry-run     # remote inspection; no page or attachment writes
docmost-publish               # show plan, ask for root title, then rebuild
```

For unattended publication:

```sh
docmost-publish --yes --no-input
```

`--yes` acknowledges replacement of the configured subtree. A noninteractive run
without it is rejected. There is no additional remote conflict prompt: the local
folder is authoritative. `--dry-run` may log in and cache a session, but does not
create, modify, upload, or delete pages/files and does not run the write probe.

Equivalent subcommands are `check`, `plan`, and `publish`. To inspect available
spaces:

```sh
docmost-publish spaces
# Or outside any project:
docmost-publish spaces --url https://wiki.example.com
```

For a fresh noninteractive login, the default environment variables are
`DOCMOST_EMAIL` and `DOCMOST_PASSWORD`. Their names can be configured in
`docmost.auth.email_env` and `docmost.auth.password_env`. Prefer logging in once
interactively. Do not put a literal password on a command line or in shell history.
This CLI does not implement automatic session renewal without valid credentials.

## Directory and file mapping

```text
docs/
  .docmost.yaml
  README.md
  architecture/
    README.md
    database.md
    storage/
      s3.md
  api/
    authentication.md
```

becomes:

```text
Engineering [existing space]
  ERP documentation [retained root; content from docs/README.md]
    api [generated parent page with child links]
      authentication
    architecture [content from architecture/README.md]
      storage [generated parent page with child links]
        s3
      database
```

A directory containing publishable Markdown anywhere in its subtree becomes a
page. Its configured index file supplies that page's body. An index is not
published as an additional child. Without an index, the directory page gets a
generated contents list. Asset-only and empty directories are omitted. Other
`.md` and `.markdown` files become child pages. Hidden paths are excluded.
Symlinks in the source tree (files, directories, and broken links) are silently
skipped; their targets are never traversed, read, or uploaded.
Directory traversal and sibling creation are deterministic: directories first,
then files, each sorted by name (case-insensitive, with exact spelling as tie-break).
This is a deterministic publisher order, not a copy of any existing manual order.

Page titles are the exact file and directory names, unchanged: same spelling,
same capitalization, without the `.md`/`.markdown` extension
(`0006-tasks-module.md` publishes as `0006-tasks-module`). Headings and front matter never rename a page. The
configured root title names the root page. Front matter is stripped from
published bodies.

## Links and attachments

Pages are created before their final bodies. An in-memory map rewrites local
Markdown links to the new Docmost URLs, including links to directory indexes and
parent directories. The local source files are never edited. Reference links are
expanded to inline links. Leading `/` in a local link means the local publication
root, not the Docmost web root. Use a full HTTPS URL for an existing remote page.

Only explicitly linked local assets are uploaded. Uploads are associated with the
owning page and deduplicated by content hash within that page for that run. The
same asset used on two pages is uploaded twice. Asset bytes are snapshotted before
remote mutations. HTTP(S) links are kept as remote links; the publisher does not
fetch or upload their targets. Files outside the source folder are never uploaded.

Defaults allow PNG, JPG/JPEG, GIF, WEBP, AVIF, BMP, PDF, TXT, and CSV. Configure
`assets.allowed_extensions` to extend this deliberately. Markdown itself always
becomes a page, not an attachment. Asset size defaults to 20 MiB; server or reverse
proxy limits may be lower. SVG is not included in the default allowlist.

Links and images pointing to symlinks, or through symlinked directories, are
silently left with their original URL; their targets are never read or uploaded.
Other links and images that cannot be resolved (missing or excluded targets, paths
escaping the source, unsupported schemes, non-allowlisted or oversized attachments)
never stop publication: they are reported as warnings and published with their
original URL, and their targets are never read or uploaded. The same
applies to raw HTML with local links/images, CSS `url()`, or `srcset`. Unsupported
wiki-link syntax is not resolved. Oversized Markdown, duplicate indexes,
invalid/duplicate YAML keys, and an unexpectedly empty tree still stop local
validation before login/deletion.

Markdown is parsed and reserialized, not preserved byte-for-byte. CommonMark,
tables, task lists, strike-through, footnotes, math, and fenced-code serialization
are implemented; Docmost's own importer/editor determines what renders there.
Do not assume every GitHub/MkDocs/Obsidian extension survives conversion.

Heading fragments are preserved with a warning, not translated into Docmost
heading IDs. Set `links.fragment_policy: drop` to remove fragments instead.
Local cross-page links are rebuilt, but links from unrelated Docmost pages,
external bookmarks, comments, history, shares, and per-page permissions are not
transferred to recreated child pages.

## Safety and failure behavior

A publication does the following:

1. Snapshot and validate the local Markdown and referenced assets.
2. Validate the login, resolve the destination, enumerate accessible descendants,
   check limits, print a plan, and obtain explicit confirmation unless `--yes`.
3. Create a root when needed. Create a temporary parent/child probe under it;
   test Markdown replacement, read-back, JSON clearing, and recursive trash.
   The probe is itself moved to Trash. It runs before any original child deletion.
4. Re-enumerate the old hierarchy and refuse unexpected structural changes.
5. Trash each original direct-child subtree, clear the retained root body,
   create the new hierarchy, upload assets, and write bodies (root last).
6. Read back page metadata/body presence and verify the final hierarchy.

Read-back verifies title, parent, space, and empty/nonempty structured content;
it is not a byte-for-byte content assertion or a visual rendering test. The probe
additionally verifies its known text. Limits and enumeration apply to what the
publishing account can see, not inaccessible restricted branches.

The run is **not atomic** and does not offer rollback. A failure after deletion
can leave an empty or partial tree. A request timeout can mean the server wrote
the change even though the client did not receive the response. Writes are never
automatically retried. Inspect the journal and destination; a deliberate new run
can discard the partial tree and rebuild it. The journal records pending and
completed operations but is not a transaction log, full backup, or resume engine.

Journals and local locks live under:

```text
~/.local/state/docmost-publisher/runs/
~/.local/state/docmost-publisher/locks/
```

`XDG_STATE_HOME` and `--state-dir` can change the base. Journals may contain
confidential document metadata and the previous root's structured body; protect
them. They do not store credentials and are not needed for subsequent publishing.
They have no automatic retention limit. Locks prevent overlapping local runs by
this user; they are not distributed locks and do not block remote editors.

The app uses **Trash only**. It does not permanently delete anything or empty
Trash. Old descendants, probe pages, and their attachments can accumulate there.
Old attachments owned by the retained root can also remain after body replacement;
they are not automatically garbage-collected by this CLI. Plan retention/cleanup
separately. Only visible page content/hierarchy is replaced; root comments,
metadata, and history are not reset.

Use HTTPS. A private CA can be configured with `docmost.http.ca_bundle` (path
relative to the config file). There is no insecure TLS-skip option. A deliberately
HTTP-only test/LAN deployment requires `docmost.http.allow_http: true`; this sends
the password and session cookie without transport encryption. Never enable it
for an untrusted network. The HTTP client ignores `.netrc` and environment proxy
settings to avoid silently changing its authentication or network route.

## Additional limits/settings

The complete supported schema and default values are in
`src/docmost_publisher/engine.py`, in `DEFAULTS`. Unknown keys and invalid types are
rejected rather than silently ignored. Selected defaults:

```yaml
publish:
  max_pages: 2000
  max_delete_pages: 2000
  max_depth: 40
  verify: true
  verify_attempts: 10
  preflight_write_probe: true
source:
  max_markdown_bytes: 5242880
  max_total_bytes: 268435456
assets:
  enabled: true
  max_bytes: 20971520
docmost:
  http:
    connect_timeout: 10
    read_timeout: 90
    request_delay_ms: 100
```

Merge these settings into the corresponding sections of your existing config;
do not duplicate YAML keys. `allow_empty: false` rejects a folder with no Markdown
files, not an intentionally empty Markdown file. An empty README clears its page
body using a valid empty structured document instead of an empty Markdown string.

Exit codes: `0` success, `1` validation/authentication/publication failure, `2`
command-line usage error, `130` interruption. No background daemon or scheduler is
installed. Run the command yourself or invoke it through your existing automation.

## Run the tests

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -q
```

Tests include a loopback HTTP mock exercising actual requests, cookies, disk
session persistence, CLI configuration, nested tree rebuilding, pagination,
attachments, destructive scoping, and failure journals. This is not live Docmost
integration testing. Start with a disposable root on your server, inspect it in
the browser, and repeat the compatibility test after a Docmost upgrade.

## Package contents

```text
install.sh                      Per-user virtualenv installer
pyproject.toml                  Package metadata and docmost-publish entrypoint
src/docmost_publisher/cli.py     CLI/config discovery and project commands
src/docmost_publisher/auth.py    Login, session reuse, and logout
src/docmost_publisher/storage.py Owner-only session storage
src/docmost_publisher/engine.py  Config validation, HTTP client, publication engine
src/docmost_publisher/markdown_tree.py  Markdown/tree/link/asset handling
tests/                          Unit and local HTTP-mock tests
examples/docs/                  Complete nested sample project
API_CONTRACT.md                  Version-dependent upstream API assumptions
TEST_REPORT.md                  Test environment and results
```

## Uninstall the installer-based installation

```sh
rm "$HOME/.local/bin/docmost-publish"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/docmost-publisher"
```

This does not remove sessions or journals. Run `docmost-publish logout` before
uninstalling to revoke a cached session. Handle any remaining session profiles and
sensitive journals separately. No changes to Docmost are made by uninstalling.

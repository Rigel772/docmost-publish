# Docmost internal API contract

This adapter targets the following upstream source interfaces inspected on
2026-09-30. No installed Docmost version was supplied. These references use the
moving `main` branch, not a pinned release or a compatibility guarantee. The
publisher intentionally fails on unknown response/pagination formats.

The built-in write probe runs under the user's chosen root before any original
child deletion. It does not test every Markdown extension, account restriction,
reverse proxy configuration, or file-upload limit. Use a disposable root first.

## Authentication

`POST /api/auth/login` with JSON `email` and `password` returns a Set-Cookie header
for `authToken`. A normal session uses the cookie, not an enterprise API key.
`POST /api/auth/logout` clears the cookie and revokes a tracked session when a
session ID is present. The JWT strategy reads the cookie, checks expiration, and
checks a tracked session's active status.

Source references:

```text
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/auth/auth.controller.ts
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/auth/strategies/jwt.strategy.ts
```

## Spaces and hierarchy

`POST /api/spaces` lists spaces accessible through account membership.
`POST /api/pages/sidebar-pages` accepts a `spaceId` for top-level pages, or `pageId`
for direct children. It is paginated. Page rows include `id`, `slugId`, `title`,
`spaceId`, and `parentPageId`. The adapter expects a standard `data` envelope,
then `{items, meta: {hasNextPage, nextCursor}}`. It follows every cursor, including
through empty filtered batches. A response without this contract is an error.

```text
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/space/space.controller.ts
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/page/page.controller.ts
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/page/services/page.service.ts
```

## Page creation and replacement

`POST /api/pages/create`: `spaceId`, optional `parentPageId`, `title`, `content`,
`format`. The publisher initially uses a minimal JSON document and expects a page
record. `POST /api/pages/update`: `pageId`, `title`, `operation: replace`, content,
and `format: markdown` (or JSON for intentionally empty content).

The service routes content changes through the collaboration layer. Its current
truthiness check on content is why this adapter does not try to clear a page by
sending an empty Markdown string.

`POST /api/pages/info` with `pageId` and `format: json` reads structured content
and metadata for sanity checks and the compatibility probe.

```text
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/page/dto/create-page.dto.ts
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/page/dto/update-page.dto.ts
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/page/services/page.service.ts
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/page/page.controller.ts
```

## Trash and assets

`POST /api/pages/delete` with `pageId` and `permanentlyDelete: false` requests trash,
not permanent deletion. The controller enforces edit access. The adapter requires
recursive trash behavior and tests it on a newly created parent/child probe;
unknown behavior stops publication before deleting old child subtrees.

`POST /api/files/upload` accepts multipart form fields `pageId` and file part
`file`. The page field is sent before the file; the upload handler reads it from
multipart fields. The adapter expects an attachment response with `url` (the upload
handler returns a bare object, unlike the ordinary data envelope).

```text
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/page/page.controller.ts
https://raw.githubusercontent.com/docmost/docmost/main/apps/server/src/core/attachment/attachment.controller.ts
```

Generated page links use `/s/<space-slug>/p/<title-slug>-<page-slugId>`. The CLI
constructs these from returned IDs and slugs. Confirm navigation and fragment
behavior on your installed UI. Relative links are rewritten; heading fragments
are not mapped to editor-specific IDs.

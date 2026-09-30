# Test report

Executed on 2026-09-30 in a Linux container with Python 3.13.5.

## Results

- Automated test cases: 57.
- Failures: 0.
- Errors: 0.
- Skipped: 0.
- Bash installer syntax: passed (`bash -n install.sh`).
- Python source compilation: passed (`python -m compileall -q src`).
- Python wheel build: passed (setuptools, without build isolation or dependency downloads).
- Wheel installed into a disposable virtual environment: passed.
- Installed entrypoint `docmost-publish --version`: passed.
- Installed entrypoint `docmost-publish --check` on the bundled nested example: passed;
  5 Markdown files produced a plan of 7 pages including the retained root.

The installation smoke test reused dependency packages already present in the
container via PYTHONPATH because external package downloads were unavailable.
The ordinary per-user installer was syntax-checked, not exercised end-to-end with
fresh dependency downloads or on CachyOS itself.

## Coverage

Unit tests cover configuration, Markdown hierarchy, directory indexes, front
matter, link rewriting, image snapshots, formatting, exclusions, validation
failures, pagination, response envelopes, token storage/permissions, URL/profile
isolation, session expiry, cookie reuse, and no automatic write retries.

HTTP-mock integration tests use an actual loopback HTTP server and the real
requests client. They cover login Set-Cookie handling, two full rebuilds with one
login, root identity retention, child ID replacement, preservation of an unrelated
root, paginated enumeration, multipart uploads, local-only checks, dry runs,
configuration discovery, initialization, logout, and recorded partial failures.
A separate test verifies that missing recursive trash support stops the run before
original children are removed.

## Not verified

No tests were run against a live Docmost instance, the user's server, or the user's
CachyOS machine. The mock is not Docmost and does not validate actual editor
rendering, all Markdown extensions, production permissions, server limits, or
collaboration persistence. Validate first in a disposable root on the real server.

The code's runtime preflight is an additional compatibility check, not a substitute
for a deployment test or a guarantee of atomic publication.

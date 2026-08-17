# Changelog

All notable changes to this project.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> **This file was reconstructed from git history and tags on 2026-08-15.** It did
> not exist before. Entries below are derived from commit messages and the code they
> touched, not from contemporaneous release notes, so they describe *what changed*
> rather than a curated release narrative.
>
> **Version numbering here is drifted and you should not trust it.** See the
> [Versioning](#versioning) note at the bottom before quoting a number.

## [Unreleased]

Everything since `v1.4.1` (2026-04-21) is untagged, so the tip is materially ahead of
the last tag.

### Security

- **BREAKING. Every route except `GET /health` now requires a shared secret.**
  Set `CCAPI_TOKEN` and send it as `Authorization: Bearer <token>` or
  `X-CCAPI-Token: <token>`. Compared with `hmac.compare_digest`, never `==`.

  Until now there was no authentication of any kind: no code path anywhere in the
  repository inspected a header, token, or origin, and the `127.0.0.1` bind was the
  entire access control. Because the server spawns
  `claude --print --dangerously-skip-permissions`, that meant **any local process
  could execute arbitrary code as the service account** through an HTTP request body,
  and could reach the OAuth access token and durable refresh token in
  `~/.claude/.credentials.json`. This was confirmed empirically on 2026-08-15, not
  inferred.

  **Every existing client breaks until it is given the secret.** Follow the cutover
  in [`SOP.md` section 5](SOP.md#5-release--deploy); it is ordered so nothing fails
  mid-flight. For Hermes that is one line: `api_key` on the `claude-code` provider.

- **Fail-closed when `CCAPI_TOKEN` is unset.** Every authenticated route returns
  `401`; there is no permissive fallback and no default value. A blank or
  whitespace-only value counts as unset. The process still starts, so `/health` stays
  answerable and the 401 body names the variable to set: exiting at boot would loop
  forever under `Restart=always` with no `StartLimitBurst` and take the one
  diagnosable endpoint down with it. Rationale in [`SECURITY.md`](SECURITY.md).

- **Removed `server.js`, `package.json`, and `package-lock.json.`** The legacy Node
  implementation was never deployed (no process, no unit, and the `/opt` path its old
  unit named does not exist) and did not receive the token gate. Leaving a second,
  unauthenticated copy of this server in a public repository was a hazard on its own.
  `package.json` existed only for it, and its `scripts.test` was still the `npm init`
  placeholder that exits 1.

- **`GET /health` bounds what it echoes.** It is the one unauthenticated route, and
  `model_discovery.last_error` is an arbitrary SDK exception string, so it is now
  truncated to `HEALTH_ERROR_MAX_CHARS` (200). The journal keeps the full text. The
  response also gained `auth`, which reports `"required"` or `"unconfigured"` and
  never contains the secret.

### Added
- `test_auth.py`: 20 tests over the token gate, covering unset, blank, missing,
  wrong, prefix and suffix variants of the token, both accepted headers, the
  streaming path, and every route including the unprefixed aliases. No network, no
  subprocess, no credentials file.
- `.github/workflows/ci.yml`: **the first workflow in this repository that executes
  the code.** Runs the tests on Python 3.11 and 3.12, then negative-controls them by
  detaching the middleware and asserting the suite goes red, then greps for a
  hardcoded `CCAPI_TOKEN`.
- `EnvironmentFile=-%h/.config/claude-code-api.env` in `claude-code-api.service`, so
  the secret lives in a `0600` file outside the checkout rather than in an
  `Environment=` line that `systemctl show` prints in the clear. The leading `-`
  keeps a missing file from becoming a boot loop.
- `SOP.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`,
  and `LICENSE`. The repository is public and previously had none of them.
- `LICENSE` (ISC), matching the `license` field `package.json` declared before it was
  removed.
- `.github/workflows/docs-check.yml`.
- `claude-opus-4-8` support, and hourly model auto-discovery from the Anthropic API
  so a newly released model appears without a code change (`29c40af`).

### Changed
- **`docs-check` promoted from tiers `1,2` to `1,2,3`,** so the `docs-evidence` block
  in `SOP.md` is now executed rather than merely present. It had already rotted while
  nothing ran it: PR #3 changed `DEFAULT_MODEL` and the `/health` payload, two checks
  stopped being true, and the gate stayed green through all of it. Both are fixed, the
  block is extended to cover the auth surface, and every check in it has been
  negative-controlled.
- `DEFAULT_MODEL` is now `claude-opus-5` (`ed3ebe4`), previously `claude-opus-4-8`
  (`ee4534f`). `README.md` and `SOP.md` still said `claude-opus-4-8`; corrected.
- `REQUEST_TIMEOUT` lifted to 1800 s, and concurrency made configurable through
  `CCAPI_MAX_CONCURRENT`, default 3 (`b247de8`). The server is no longer serialised
  to one call at a time.
- MCP servers are no longer booted inside each spawned subprocess by default, roughly
  halving per-request latency. Set `CLAUDE_API_LOAD_MCP=1` to restore the old
  behaviour (`818c3a2`).
- `claude-code-api.service` rewritten to describe the real deployment: a systemd
  **user** unit running `server.py` on port 18782. It previously described a
  system-scope Node deployment out of `/opt/claude-code-api` on port 3456, which has
  never been how this is run. Its `User=%i` was also inert, because `%i` only expands
  in template units.
- `README.md` corrected against the code. See the docs pull request for the full list;
  the largest were the `/health` response shape, the "single-threaded queue" claim,
  and a client configuration section for a runtime that is no longer in use.

### Security
- Documented, for the first time, that this is an **unauthenticated endpoint that can
  execute arbitrary commands** as the user running it, and that it reads a live OAuth
  access token (and sits beside a refresh token) at `~/.claude/.credentials.json`.
  The behaviour is unchanged; it was simply never written down. See `SECURITY.md`.
- Full-history secret scanning via the gitleaks binary on every push (`a7e2746`,
  merged in `e3fd9ee`).
- Enabled GitHub private vulnerability reporting on the repository.

## [1.4.1] - 2026-04-21
### Added
- `claude-opus-4-7`, set as the default Opus model (`a7db155`).

### Changed
- Routine `server.py` maintenance (`0e1155a`).

## [1.4.0] - 2026-04-04
### Added
- Vision and image support. Requests containing `image_url` content blocks bypass the
  CLI and call the Anthropic SDK directly, authenticating with the OAuth access token
  from `~/.claude/.credentials.json` (`00a6a47`).

## [1.1.2] - 2026-04-04
### Fixed
- Prompts are piped to the subprocess on stdin instead of passed as an argument,
  fixing `[Errno 7] Argument list too long` on large contexts (`1c90c55`).

## [1.1.1] - 2026-04-04
### Changed
- `--dangerously-skip-permissions` enabled on every `claude` invocation (`455f324`).
  This is the single most security-relevant commit in the repository: it is what
  makes a request body able to run commands. It shipped with no accompanying
  documentation, which this release cycle corrects.

## [1.1.0] - 2026-04-04
### Added
- `server.py`, the Python/aiohttp implementation, alongside the original Node server.
  Fixes the stream-json timeout by always using `--output-format json` and chunking
  the completed response into SSE (`788d0b3`).

## [1.0.0] - 2026-04-03/04
### Added
- Initial Node/Express wrapper exposing an OpenAI-compatible endpoint over
  `claude --print` (`697a7eb`, `9f2d9a7`).
- `.gitignore`, and `node_modules/` removed from tracking (`b26223d`).

---

## Versioning

**There is no source of truth for this project's version.**

- Git tags run `v1.1.0`, `v1.1.1`, `v1.1.2`, `v1.4.0`, `v1.4.1`. Note the gap:
  `1.2.x` and `1.3.x` were never tagged.
- The current tip is well past `v1.4.1` and untagged.
- There are no GitHub releases and no published npm or PyPI package. Nothing derives a
  version from a tag, so a tag records a point in history and nothing more.
- `package.json` was the other claimant. It said `"version": "1.0.0"`, had never been
  bumped since the initial commit, and was **removed on 2026-08-16** along with the
  Node implementation it described. The disagreement is therefore resolved by
  subtraction, not by reconciliation.

Do not quote a version number for this project. If a release process is ever wanted,
that is a deliberate decision to make, not a number to pick.

`integrate-20260709` is an annotated tag at `d06bad5`, not a version.

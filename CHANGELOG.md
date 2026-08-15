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

Everything since `v1.4.1` (2026-04-21) is untagged. That is 7 commits including two
feature changes, so the tip is materially ahead of the last tag.

### Added
- `SOP.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`,
  and `LICENSE`. The repository is public and previously had none of them.
- `LICENSE` (ISC), matching the `license` field `package.json` already declared.
- `.github/workflows/docs-check.yml` (tiers 1 and 2).
- `claude-opus-4-8` support, and hourly model auto-discovery from the Anthropic API
  so a newly released model appears without a code change (`29c40af`).

### Changed
- `DEFAULT_MODEL` is now `claude-opus-4-8` (`ee4534f`).
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

**There is no single source of truth for this project's version, and the two that
exist disagree.**

- `package.json` says `"version": "1.0.0"`. It has never been bumped since the initial
  commit and does not track anything.
- Git tags run `v1.1.0`, `v1.1.1`, `v1.1.2`, `v1.4.0`, `v1.4.1`. Note the gap:
  `1.2.x` and `1.3.x` were never tagged.
- The current tip is 7 commits past `v1.4.1` and untagged.
- There are no GitHub releases and no published npm or PyPI package.

Treat the **git tag** as the closest thing to a real version and `package.json` as
stale. Resolving this properly (bump `package.json`, tag the tip, or drop tags
entirely) is a follow-up, not something a documentation change should decide.

`integrate-20260709` is an annotated tag at `d06bad5`, not a version.

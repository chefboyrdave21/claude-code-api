# Contributing

Thanks for looking. This is a small, single-maintainer project: one Python file and a
test suite that covers exactly one thing, the auth gate. That shapes everything below.

## Before you start

**Read [`SECURITY.md`](SECURITY.md) first.** This project is an HTTP endpoint that
executes commands as the user running it. That is intentional, and it means an
innocuous-looking change can have outsized consequences. In particular:

- **Do not weaken the token gate.** No new route outside `auth_middleware`, no
  additions to `UNAUTHENTICATED_PATHS`, no comparison other than
  `hmac.compare_digest`, and above all no path where an unset `CCAPI_TOKEN` means
  "allow". `test_auth.py` covers each of those; if your change makes one of those
  tests fail, the test is right and the change is wrong.
- **Do not change the default bind address** away from `127.0.0.1`. The token is
  authorization, the loopback bind is containment, and neither replaces the other.
- **Do not add anything that logs, echoes, stores, or forwards** the OAuth token read
  from `~/.claude/.credentials.json`, including in an error message or a debug line.
- **Do not remove the `--strict-mcp-config` default.** Booting the host's MCP servers
  inside each subprocess widens the blast radius considerably.
- **Do not put a secret in the tree.** `CCAPI_TOKEN` comes from the environment.
  There is no default, no example value, and no fallback. `ci` greps for one and
  `secret-scan` walks the full history.
- **Do not add a third-party dependency** without saying why in the pull request. The
  attack surface here is already large enough.

If your change touches any of the above, say so explicitly in the pull request. Do not
make the reviewer find it.

## Which file to change

`server.py`. It is the one that runs. A legacy Node implementation (`server.js`) was
deleted on 2026-08-16; it was never deployed and never had the token gate.

## Verifying a change

```bash
pip install aiohttp anthropic
python3 -m unittest discover -v      # 20 tests, no network, no subprocess
```

The `ci` workflow runs that on every push and pull request, on Python 3.11 and 3.12,
and additionally proves the suite goes red when `auth_middleware` is detached.

**That covers auth and nothing else.** The request-translation, streaming, vision, and
discovery paths have no automated coverage, because exercising them means calling the
real `claude` CLI. For a change touching any of those, run the manual checklist in
[`SOP.md` section 4](SOP.md#4-test) on a scratch port and **paste the output into your
pull request**.

Extending coverage is the most valuable contribution this repository could receive.
`normalise_model`, `messages_to_prompt`, and `_openai_content_to_anthropic` are pure
functions that need no subprocess, and `test_auth.py` already shows how to drive the
app with stubbed collaborators.

## Documentation

`README.md`, `SOP.md`, and `SECURITY.md` are expected to match the code. The
`docs-check` workflow enforces the cheap parts of that:

- **Tier 1** requires the seven standard docs to exist.
- **Tier 2** requires a `CHANGELOG.md` entry alongside a code change.
- **Tier 3** executes the `docs-evidence` block at the end of `SOP.md`.

That block is a set of `grep` assertions pinning documented values (the token env var
and header names, the unauthenticated-path allowlist, the constant-time comparison,
the port, the default model, the `/health` shape, the credential path, the permission
flag, the concurrency default, the bind address, the unit's `ExecStart`) to the lines
that define them. **If you change one of those values, the matching assertion must
change with it,** or CI goes red.

Tier 3 was only promoted on 2026-08-16. Before that the block existed but nothing ran
it, and it quietly went stale: two checks had been false since PR #3 and docs-check
stayed green the whole time. If you add a check, break the fact and confirm the check
reports non-zero before you trust it.

You can run the whole thing locally:

```bash
python3 docs_check.py --repo . --tier 1 --tier 3   # must print RESULT: pass
```

## Commits and pull requests

- Conventional-commit prefixes, matching the existing history: `feat:`, `fix:`,
  `perf:`, `docs:`, `ci:`, `chore:`.
- Update `CHANGELOG.md` under `## [Unreleased]` for anything user-visible.
- One logical change per pull request.
- Do not push tags. The version story here is already tangled (see
  [`CHANGELOG.md`](CHANGELOG.md#versioning)); leave it to the maintainer.

## Reporting bugs

Normal bugs: open a GitHub issue with the request that triggers it, the response you
got, and the relevant `journalctl --user -u claude-code-api` lines.

Security issues: **not** a public issue. See [`SECURITY.md`](SECURITY.md). Check its
"Out of scope" list first, because the things that most look like vulnerabilities here
are documented, intended behaviour.

## License

Contributions are accepted under the [ISC License](LICENSE), the same terms the
project ships under.

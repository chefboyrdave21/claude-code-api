# Contributing

Thanks for looking. This is a small, single-maintainer project: one Python file, one
legacy Node file, and no test suite. That last part shapes everything below.

## Before you start

**Read [`SECURITY.md`](SECURITY.md) first.** This project is an unauthenticated HTTP
endpoint that executes commands as the user running it. That is intentional, and it
means an innocuous-looking change can have outsized consequences. In particular:

- **Do not change the default bind address** away from `127.0.0.1`. It is the only
  access control this project has.
- **Do not add anything that logs, echoes, stores, or forwards** the OAuth token read
  from `~/.claude/.credentials.json`, including in an error message or a debug line.
- **Do not remove the `--strict-mcp-config` default.** Booting the host's MCP servers
  inside each subprocess widens the blast radius considerably.
- **Do not add a third-party dependency** without saying why in the pull request. The
  attack surface here is already large enough.

If your change touches any of the above, say so explicitly in the pull request. Do not
make the reviewer find it.

## Which file to change

`server.py`. It is the one that runs.

`server.js` is unmaintained legacy: it is not deployed anywhere, and it has drifted on
models and features. A fix there does not reach anything. Change it only if you
specifically use the Node version, and say so.

## Verifying a change, without a test suite

There is no CI that runs this server. `npm test` is still the `npm init` placeholder
that exits 1, and no workflow executes `server.py`. **Nothing mechanically stops a
broken change from being merged**, so manual verification is the gate.

Run the four-step checklist in [`SOP.md` section 4](SOP.md#4-test) on a scratch port
and **paste the output into your pull request**. A pull request touching `server.py`
with no evidence it was run will be asked for it.

If you would like to fix the real problem, a pull request that adds an actual test
suite is the most valuable contribution this repository could receive right now. Even
a handful of `pytest` cases over `normalise_model`, `messages_to_prompt`, and
`_openai_content_to_anthropic` (all pure functions, no subprocess needed) would be a
meaningful improvement, and a `pytest` workflow could then be added as a gate that
starts green.

## Documentation

`README.md`, `SOP.md`, and `SECURITY.md` are expected to match the code. The
`docs-check` workflow enforces the cheap parts of that:

- **Tier 1** requires the seven standard docs to exist.
- **Tier 2** requires a `CHANGELOG.md` entry alongside a code change.

`SOP.md` also carries a `docs-evidence` block: a set of `grep` assertions pinning
documented values (the port, the default model, the `/health` shape, the credential
path, the permission flag, the concurrency default, the bind address) to the lines of
`server.py` that define them. If you change one of those values, **the matching
assertion in `SOP.md` must change with it.** The block is not yet run as a gate
(`docs-check.yml` is at tiers `1,2`), so for now it is on you.

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
"Out of scope" list first, because the two things that most look like vulnerabilities
here are documented, intended behaviour.

## License

Contributions are accepted under the [ISC License](LICENSE), the same terms the
project ships under.

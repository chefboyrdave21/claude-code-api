# Security Policy: claude-code-api

> ## Read this before you run it
>
> `claude-code-api` is an **unauthenticated HTTP endpoint that executes shell
> commands and reads and writes files as the user who started it.**
>
> That is not a bug report, it is the design. The server spawns
> `claude --print --dangerously-skip-permissions` (`server.py:271-277`), which
> disables every tool-permission prompt. Claude Code's built-in Bash, Read,
> Write, and Edit tools remain available in `--print` mode, so **the body of an
> HTTP request is an instruction that can run arbitrary code.** There is no API
> key, no token, no allowlist, and no per-request authorization anywhere in this
> repository.
>
> **Bind it to loopback and keep it there.** It is safe only to the extent that
> nothing untrusted can reach the socket and nothing untrusted can reach the
> prompt.

This was confirmed empirically on 2026-08-15, not inferred. A single unauthenticated
`POST /v1/chat/completions` to the running loopback service, whose message body asked
for a shell command, returned the live output of that command: the service account
name, the hostname, and the working directory of the server process. Any command that
account can run, a request body can run.

---

## Threat model

The trust boundary is the **socket**, and nothing else. Anything that can open a TCP
connection to the listen address has the full authority of the account running the
service.

### In scope

- **Anything that widens the socket.** A default bind that is not `127.0.0.1`, a
  documented deployment that exposes it on a LAN, tailnet, or public interface, a
  reverse proxy example that forwards to it without auth, or a container recipe that
  publishes the port.
- **Credential exposure.** `server.py:35` reads
  `~/.claude/.credentials.json` and `server.py:344-348` extracts
  `claudeAiOauth.accessToken`, which is then passed as `api_key` to the Anthropic SDK
  (`server.py:329-330` for hourly model discovery, `server.py:358-359` for the vision
  path). Any change that logs that token, echoes it in an error body, writes it to
  disk, or sends it anywhere other than `api.anthropic.com` is a vulnerability.
  Note that the credentials file also holds a **refresh token**, so read access to it
  is durable access, not a short-lived leak.
- **Prompt-reflected exfiltration.** The subprocess inherits the server's environment
  and home directory. A request that causes secrets from the host (SSH keys, other
  credential files, environment variables) to be returned in the completion body is in
  scope, and is the most likely real-world exploitation path.
- **A path that escalates beyond the invoking account**, for example a setuid helper,
  a writable unit file, or a sudo rule reachable through the subprocess.
- **Resource exhaustion** past what `CCAPI_MAX_CONCURRENT` and `QUEUE_TIMEOUT` bound,
  or a request that pins the box for the full 1800s `REQUEST_TIMEOUT` without
  releasing the semaphore.
- **Secrets committed to this repository.** The `secret-scan` workflow scans the full
  history with the gitleaks binary on every push.

### Out of scope

- **"The endpoint has no authentication."** Known, documented above, and by design for
  a loopback-only helper. A patch that adds an optional shared-secret or capability
  header is a welcome pull request, but it is a feature request, not a vulnerability
  report.
- **"`--dangerously-skip-permissions` is dangerous."** Also known and documented. It
  is why the loopback bind is load-bearing.
- **Vulnerabilities in the `claude` CLI, the `anthropic` SDK, `aiohttp`, or Express.**
  Report those to their own projects.
- **Anthropic account, subscription, or rate-limit policy.** This wrapper routes
  inference through an existing Claude Code subscription; whether that is permitted
  for your use is between you and Anthropic's terms.
- **`server.js`.** The legacy Node implementation is unmaintained and is not deployed
  anywhere (see `SOP.md`). It shares the `--dangerously-skip-permissions` posture. It
  will be accepted as a report only if you can show something running it.

---

## Deployment guidance

| Control | Status in this repository |
|---|---|
| Listen address | Defaults to `127.0.0.1` (`server.py:546`). **Do not change this.** |
| Authentication | **None.** There is no auth layer of any kind. |
| Transport encryption | None. Loopback only, so none is needed. Adding TLS without adding auth solves nothing. |
| Tool permission prompts | **Disabled** via `--dangerously-skip-permissions`. |
| MCP servers in the subprocess | **Disabled by default** (`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`). Setting `CLAUDE_API_LOAD_MCP=1` re-enables the host's configured MCP servers inside the wrapper, which **widens the blast radius** from local tools to every tool those servers expose. Treat that env var as a security setting. |
| Session persistence | Disabled via `--no-session-persistence`, so a prompt cannot poison a later request through saved session state. |
| Credential file permissions | `~/.claude/.credentials.json` is expected to be `0600`. The server does not create or chmod it. |

If you need this reachable from another host, terminate an authenticating proxy in
front of it and keep the wrapper itself on loopback. Do not change the bind address.

---

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

- **Primary:** GitHub **private vulnerability reporting**. Go to the
  [Security tab of `chefboyrdave21/claude-code-api`](https://github.com/chefboyrdave21/claude-code-api/security)
  and choose "Report a vulnerability". This channel is enabled on this repository.
- **Secondary:** contact the maintainer through the GitHub profile.

Please include: which server you ran (`server.py` or `server.js`), the listen address
you used, the request that triggers it, and what you observed. Read the **Out of
scope** list first, because the two things that most look like vulnerabilities here
are already documented as intended behaviour.

We aim to **acknowledge within 72 hours**, and to ship a fix or a documented
mitigation within 90 days, coordinating a disclosure date with you.

**Safe harbour:** good-faith research conducted under coordinated disclosure will not
be pursued. Test against **your own** instance only. Do not attempt to reach anyone
else's deployment, and do not use a report as a pretext to consume someone else's
subscription quota. Credit is given unless you ask otherwise.

---

## Supported versions

There are no releases, no tags, and no published package. `package.json` carries a
static `1.0.0` that has never been bumped and does not track anything.

| Ref | Status |
|---|---|
| `main` | The only supported ref. Fixes land here. |
| `server.py` | Maintained. This is what actually runs. |
| `server.js` | **Unmaintained legacy.** Not deployed, and behind on models and features. Security fixes are not backported to it. |

---

**License:** ISC (see [`LICENSE`](LICENSE)).
**Standards:** ISO/IEC 29147 and 30111 (vulnerability disclosure).

# Security Policy: claude-code-api

> ## Read this before you run it
>
> `claude-code-api` is an **HTTP endpoint that executes shell commands and reads
> and writes files as the user who started it.**
>
> That is the design, not a bug. The server spawns
> `claude --print --dangerously-skip-permissions`, which disables every
> tool-permission prompt. Claude Code's built-in Bash, Read, Write, and Edit tools
> remain available in `--print` mode, so **the body of an HTTP request is an
> instruction that can run arbitrary code.**
>
> Since 2026-08-16 that endpoint requires a **shared secret** on every route
> except `GET /health`. Set `CCAPI_TOKEN`, or the service refuses every request.
> **Keep it on loopback anyway.** The token is authorization; the loopback bind
> is containment. Neither replaces the other.

This is not theoretical. It was confirmed empirically on 2026-08-15: a single
**unauthenticated** `POST /v1/chat/completions` to the running loopback service, whose
message body asked for a shell command, returned the live output of that command,
including the service account name, the hostname, and the working directory of the
server process. Any command that account can run, a request body can run. That is what
the token now gates.

---

## Authentication

| | |
|---|---|
| Secret source | `CCAPI_TOKEN` environment variable. Nothing else is consulted. |
| Accepted headers | `Authorization: Bearer <token>` **or** `X-CCAPI-Token: <token>` |
| Comparison | `hmac.compare_digest` on the UTF-8 bytes, so a wrong token cannot be recovered from response timing. Never `==`. |
| Guarded | Every route: `/v1/chat/completions`, `/v1/models`, and their unprefixed aliases `/chat/completions` and `/models`. |
| Open | `GET /health` only, enforced by an allowlist (`UNAUTHENTICATED_PATHS`) rather than per-route decoration, so a route added later is guarded by default. |
| Failure code | `401` in the OpenAI error shape, so OpenAI-compatible clients surface it as an auth failure rather than a generic 500. |

**Both accepted headers exist for a reason.** `Authorization: Bearer` is what an
OpenAI-compatible client already sends for its configured `api_key`, so a consumer
needs a credential set and no custom header plumbing. `X-CCAPI-Token` is the escape
hatch for a client that cannot set `Authorization`. Prefer `Bearer`: a client that can
only inject a *global* custom header would attach `X-CCAPI-Token` to every provider it
talks to, sending this secret to unrelated hosts.

### What happens when `CCAPI_TOKEN` is unset

**Every authenticated route returns 401.** There is no configuration under which a
missing token means "allow". A blank or whitespace-only value counts as unset and is
never treated as a secret that a blank header could match.

The process still **starts** in that state, which is a deliberate choice between two
fail-closed designs:

- *Refuse to start* would be equally closed, but the shipped unit is
  `Restart=always` with **no** `StartLimitBurst`, so exiting at boot produces a
  restart loop that never stops and takes `/health` down with it. The operator is
  then left with no endpoint to ask what is wrong.
- *Refuse every request* reaches the subprocess exactly as rarely (never), keeps
  `/health` answering with `"auth": "unconfigured"`, and returns a 401 body that names
  the variable to set. It logs the same at `ERROR` on every refusal and once at
  startup.

Nothing is served in the unconfigured state. The difference is only whether the
failure is diagnosable.

### Generating and storing the secret

```bash
umask 077
printf 'CCAPI_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  > ~/.config/claude-code-api.env
chmod 600 ~/.config/claude-code-api.env
```

The unit reads it with `EnvironmentFile=-%h/.config/claude-code-api.env`. Do **not**
put the value in an `Environment=` line: `systemctl show` and `systemctl cat` print
those in the clear to anyone who can read the unit. Do **not** commit it: the `ci`
workflow greps for a hardcoded `CCAPI_TOKEN` value and `secret-scan` walks the full
history.

Rotation is: rewrite the env file, restart the unit, update every consumer. There is
no dual-token grace window, so consumers break between those steps. Sequence it.

### What the token does not do

- **It is not multi-tenancy.** One secret, one privilege level, no identities, no
  scopes, no per-caller audit trail. Anyone holding it can run anything the service
  account can run.
- **It is not rate limiting.** `CCAPI_MAX_CONCURRENT` and `QUEUE_TIMEOUT` are the only
  bounds, and they apply after authentication.
- **It does not make the port safe to expose.** An authenticating proxy in front of a
  loopback bind is still the only supported way to reach this from another host.
- **It is not transport security.** There is no TLS. On loopback that is fine; over a
  network the token would cross in cleartext.

---

## Threat model

The trust boundary is the **socket plus the token**. Anything that can open a TCP
connection to the listen address **and** present the shared secret has the full
authority of the account running the service. Before 2026-08-16 the socket alone was
sufficient.

### In scope

- **Anything that widens the socket.** A default bind that is not `127.0.0.1`, a
  documented deployment that exposes it on a LAN, tailnet, or public interface, a
  reverse proxy example that forwards to it without auth, or a container recipe that
  publishes the port.
- **Anything that weakens or bypasses the token gate.** A route registered outside the
  middleware, an addition to `UNAUTHENTICATED_PATHS`, a comparison that is not
  `hmac.compare_digest`, a fallback that treats an unset `CCAPI_TOKEN` as permissive,
  or a default value for it anywhere in the tree. `test_auth.py` covers each of these
  and the `ci` workflow proves the suite goes red when the middleware is detached.
- **Credential exposure.** `CREDS_PATH` reads `~/.claude/.credentials.json` and
  `_get_oauth_token` extracts `claudeAiOauth.accessToken`, which is passed to the
  Anthropic SDK by `_refresh_models` (hourly model discovery) and `_run_claude_sdk`
  (the vision path). Any change that logs that token, echoes it in an error body,
  writes it to disk, or sends it anywhere other than `api.anthropic.com` is a
  vulnerability. Note that the credentials file also holds a **refresh token**, so
  read access to it is durable access, not a short-lived leak.
- **Anything sensitive added to `GET /health`,** which is unauthenticated by design.
  It currently reports status, service name, the `PORT` constant, whether auth is
  configured (**not** the secret), and model-discovery state. The discovery
  `last_error` is an arbitrary SDK exception string and is truncated to
  `HEALTH_ERROR_MAX_CHARS`; the journal keeps the full text. Widening this response is
  in scope.
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

- **"A single shared secret is weak authentication."** Known and documented above. One
  token, one privilege level, no identities. Moving to per-caller capabilities is a
  feature request, not a vulnerability report.
- **"Anyone who can read `~/.config/claude-code-api.env` gets full access."** Correct,
  and true of any local secret. Protect it with file permissions like any other
  credential. An account that can read that file can usually read
  `~/.claude/.credentials.json` too.
- **"`--dangerously-skip-permissions` is dangerous."** Also known and documented. It is
  why the loopback bind stays load-bearing even now that a token is required.
- **Vulnerabilities in the `claude` CLI, the `anthropic` SDK, or `aiohttp`.**
  Report those to their own projects.
- **Anthropic account, subscription, or rate-limit policy.** This wrapper routes
  inference through an existing Claude Code subscription; whether that is permitted
  for your use is between you and Anthropic's terms.
- **`server.js`.** The legacy Node implementation was **deleted on 2026-08-16**. It was
  never deployed, it never gained the token gate, and leaving a second unauthenticated
  copy of this server in a public repository was a hazard in its own right: someone
  would eventually run it. If you are looking at it, you are reading history. Use
  `git log -- server.js`.

---

## Deployment guidance

| Control | Status in this repository |
|---|---|
| Listen address | Defaults to `127.0.0.1`. **Do not change this.** |
| Authentication | **Required.** Shared secret in `CCAPI_TOKEN`, `hmac.compare_digest`, every route except `GET /health`. Unset means everything 401s. |
| Transport encryption | None. Loopback only, so none is needed. TLS is not a substitute for the token, and the token is not a substitute for the loopback bind. |
| Tool permission prompts | **Disabled** via `--dangerously-skip-permissions`. |
| MCP servers in the subprocess | **Disabled by default** (`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`). Setting `CLAUDE_API_LOAD_MCP=1` re-enables the host's configured MCP servers inside the wrapper, which **widens the blast radius** from local tools to every tool those servers expose. Treat that env var as a security setting. |
| Session persistence | Disabled via `--no-session-persistence`, so a prompt cannot poison a later request through saved session state. |
| Credential file permissions | `~/.claude/.credentials.json` and `~/.config/claude-code-api.env` are both expected to be `0600`. The server creates or chmods neither. |
| Secrets in the tree | `secret-scan` walks the full history with the gitleaks binary; `ci` additionally greps for a hardcoded `CCAPI_TOKEN` value. |

If you need this reachable from another host, terminate an authenticating proxy in
front of it and keep the wrapper itself on loopback. Do not change the bind address.
The shared secret raises the floor; it does not make the port safe to publish.

---

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

- **Primary:** GitHub **private vulnerability reporting**. Go to the
  [Security tab of `chefboyrdave21/claude-code-api`](https://github.com/chefboyrdave21/claude-code-api/security)
  and choose "Report a vulnerability". This channel is enabled on this repository.
- **Secondary:** contact the maintainer through the GitHub profile.

Please include: the commit you ran, the listen address you used, whether `CCAPI_TOKEN`
was set, the request that triggers it, and what you observed. Read the **Out of
scope** list first, because the things that most look like vulnerabilities here are
already documented as intended behaviour.

We aim to **acknowledge within 72 hours**, and to ship a fix or a documented
mitigation within 90 days, coordinating a disclosure date with you.

**Safe harbour:** good-faith research conducted under coordinated disclosure will not
be pursued. Test against **your own** instance only. Do not attempt to reach anyone
else's deployment, and do not use a report as a pretext to consume someone else's
subscription quota. Credit is given unless you ask otherwise.

---

## Supported versions

There are no releases and no published package. Git tags exist but do not track a
release process.

| Ref | Status |
|---|---|
| `main` | The only supported ref. Fixes land here. |
| Anything before `fix/authenticate-requests` merged (2026-08-16) | **Unauthenticated.** Every commit before that gate serves arbitrary code execution to any local process. Do not run one. |

---

**License:** ISC (see [`LICENSE`](LICENSE)).
**Standards:** ISO/IEC 29147 and 30111 (vulnerability disclosure).

# omp-irc-mcp

oh-my-pi (omp) over IRC through a tiny Python MCP server — **env-only config**,
a **small optimized context** for local/small models, and **one long-running
omp session** supervised in-process, with a durable task queue that survives
restarts.

```
                    ┌─────────────────────────── container ───────────────────────────┐
 IRC network        │                                                                 │
◄──── TLS ──────────┤  mcp_irc_server.py --daemon          (owns the IRC socket)      │
   nick stays up    │   │ spool: /state/irc/messages.jsonl                            │
                    │   │ unix socket: /state/irc/mcp.sock                            │
                    │   ▲                                 ▲                           │
                    │   │ spool (mentions)                │ irc_say (replies)         │
                    │   │                                 │                           │
                    │  entrypoint.sh ──► loop.py supervisor                            │
                    │     ONE omp session (persistent, small PROMPT.md context):       │
                    │     stdin  ◄─ mentions + queued task chunks                      │
                    │     stdout ─► relayed to IRC via irc_say                         │
                    │   long lane: /state/tasks/queue.jsonl — checkpointed, durable    │
                    └─────────────────────────────────────────────────────────────────┘
                          ▲ env only: IRC_*, LLM_*, LOOP_* (see .env.example)
```

## Quickstart

```sh
cp .env.example .env          # fill IRC_* + LLM_*
docker build -t omp-irc-mcp .
docker run -d --env-file .env -v omp-state:/state omp-irc-mcp
```

The user sets **only environment variables**. Two documented exceptions, both
rendered by `entrypoint.sh` from env at container start:

- omp has no env var for MCP servers → `.omp/mcp.json` (with
  IRC_NICKSERV_PASS, mode 0600) is generated under `$OMP_WORKDIR`.
- Trust is a file of host globs → `TRUST_FILE` (default `/state/trust.txt`,
  mode 0600) is generated from `IRC_TRUST_HOSTS` if not already present, and
  can be edited in place at runtime.

## One long-running session

There is exactly **one** omp process for the life of the container — a
persistent session, not one omp invocation per message:

- `entrypoint.sh` renders the MCP config, starts the IRC daemon, then runs
  `loop.py` (the supervisor, in-process — no separate brain process).
- `loop.py` starts omp once with the compact `PROMPT.md` via
  `--system-prompt` (model/provider from `LLM_*` env) and keeps it alive,
  restarting it if it ever exits.
- New trusted mentions from the daemon's spool are forwarded into the
  session's stdin; each stdout reply is sent back to IRC with `irc_say` over
  the daemon's unix socket, so the nick never flaps and no channel traffic is
  missed.
- Long work runs **in the same session**: durable tasks live in
  `/state/tasks/queue.jsonl` (append `{"id","status":"pending","prompt","checkpoint"}`).
  Between mentions the supervisor forwards the next pending task; the agent
  keeps its own `checkpoint` up to date, so the queue survives restarts and
  tasks run across days.
- The agent can enqueue work itself, so a channel request that needs real
  effort becomes a queued task instead of a blocked channel.

## Host-based trust

Trust is keyed on the sender's **IRC host**, not their nick — a nick squatter
is not trusted. `TRUST_FILE` (default `/state/trust.txt`) holds one glob per
line, matched with `fnmatch` against the user@host, like a fleet policy.json:

```
# one host glob per line; '#' starts a comment
o.o                      # operatore
152.53.49.131            # fleet agents
*.cluster.example.com    # whole suffix
```

Globs are re-read per message, so edits apply without a restart. Nicks in
`IRC_IGNORE_NICKS` and services are always dropped at the spool layer. Only
trusted mentions/DMs reach the agent.

## Why MCP?

The IRC transport is not forked into omp — it is a 5-tool MCP server
(`irc_connect`, `irc_join`, `irc_say`, `irc_channels`, `irc_recent`). The agent
decides when to read history and what to say, and swapping the transport (XMPP,
Slack) means swapping one stdio process. The `--daemon` mode owns the IRC
socket, spools channel traffic, and serves MCP over a unix socket that both
the omp session and the supervisor use.

## Context discipline (can1357/oh-my-pi#1734)

Issue 1734 measures a ~22–40k token system prompt: tool JSON schemas are 52% of
it, and a 40k prompt actively hurts small local models (maki's ~4k prompt beats
omp on Qwen 27B in that thread). What we apply from the discussion:

- **Skills-over-tools-upfront**: only 5 IRC tools are mounted; everything else is
  plain file/shell/edit. No giant `{{xdevDocs}}` dump.
- **Compact but concrete system prompt** (`PROMPT.md`, ~700 tokens): the thread's
  acceptance criteria say a compact profile must *keep* authority/safety rules,
  tool-selection rules, stop rules, and edit/test workflow — small models improvise
  tool behavior when you strip the concrete contract. Ours keeps exactly those and
  drops role prose, examples, and workflow-specific material.
- **Persistent session, static prompt prefix**: one session instead of one
  process per mention means the provider prompt cache hits on every turn; the
  shipped `--system-prompt` file is static.
- Not done (the thread rejects it): replacing tool JSON schemas with prose —
  that moves argument validation into model inference and raises malformed calls.

## Follow-ups

- CI (lint + a stdio MCP smoke test against a local ircd).
- Backpressure: per-nick reply rate limiting.
- WHOIS/NickServ verification layered on top of host globs for hostile nets.

MIT — see [LICENSE](LICENSE).

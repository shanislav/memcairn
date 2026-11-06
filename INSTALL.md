# Installing Memcairn: instructions for an AI agent

You are an AI agent (Claude Code or similar) and the user asked you to install Memcairn.
Follow this page. Talk to the user in their language, ask one short question at a time,
and explain each option in a sentence. The user may not know what MCP, a token or a
reverse proxy is; they don't need to.

## Ground rules

- **Never show, paste or ask for a token.** Machines connect with one-time pairing codes
  (`memcairn pair`); the token goes straight into the configuration. A pairing code is
  fine to see: it works once, for 10 minutes.
- **Ask before anything that downloads or installs software**, and before every change
  on a server. Back up files you change on a server. Don't open ports to the internet
  unless the user asks for it and understands it.
- **Don't edit `CLAUDE.md` and don't turn off Claude Code's auto memory.** Memcairn
  complements it: auto memory keeps how the user wants you to work, Memcairn keeps facts
  about the world and shares them between machines.
- If a step fails, show the error, say what you think it means, and ask. Don't work
  around it silently.

## 1. Look before asking

Check, without changing anything:

- the operating system;
- `uv --version` (Memcairn installs with [uv](https://docs.astral.sh/uv/));
- `claude --version` (the Claude Code CLI; the desktop app may not have it on PATH, which
  is fine);
- whether a memory server is already configured: an `mcpServers.memory` entry in
  `~/.claude.json` or a project `.mcp.json`, or `claude mcp list`. If there is one
  (often the official `@modelcontextprotocol/server-memory`), note it for step 5;
- whether this session already has Memcairn tools from a **claude.ai connector** (the
  user added Memcairn in claude.ai; Claude Code loads claude.ai connectors too). You may
  use it to look things up, such as the server address, but still connect the machine
  as below: once this machine has its own entry, Claude Code hides the connector by
  itself, so memory is not loaded twice and writes are recorded as this machine.

If uv is missing, ask, then install it:

- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Open a new terminal afterwards (or use the full path uv printed) so `uv` is found.

## 2. Ask: one computer or several?

> "Should the memory live only on this computer, or should several computers (or
> claude.ai and the Claude phone app) share it?"

- **Only this computer** (simplest, no server, nothing to keep running): go to 3A.
- **Several computers or the phone**: it needs a small server. Ask whether they already
  run a Memcairn server. Yes: go to 3C. No: go to 3B.

### Ask: full or basic search?

Ask this before installing (3A or 3B), in a sentence or two:

> "Memcairn finds things by their words. It can also find them by meaning (other words,
> another language) with a small model that runs on this machine: about 0.5 GB of disk
> and 0.6 GB of memory while in use. Without it, it needs about 0.1 GB of memory and
> searches by words only. On a normal computer or server keep it; on a Raspberry Pi or a
> small VPS you may want to save the space."

Full search is the default. For basic search: locally, install without `[vector]`; on a
server, add `MEMCAIRN_EMBED_MODEL=none` to `.env`. Either can be changed later.

## 3A. Only this computer

```bash
uv tool install "memcairn[vector]"   # full
uv tool install memcairn             # basic
memcairn connect --local
```

`connect --local` registers Memcairn in `~/.claude.json` (Claude Code starts it itself,
no network, no token) and adds a session-start hook that loads a short memory overview.
Both files are backed up first. The memory is `~/.memcairn/memcairn.db`. Go to 4.

Tell the user they can browse and edit memory with `memcairn ui` (opens the browser,
logs in by itself) and can move to a server later (copy the database with
`memcairn backup <file>`).

## 3B. Set up a server

Ask, one at a time:

1. **Which machine?** Something that is always on: a home server, NAS or small VPS that
   you can reach over SSH and that runs Docker. Ask for SSH access or ask the user to run
   the commands there.
2. **How will the other machines reach it?** In order of preference:
   - a VPN they already use (Tailscale, WireGuard): private, nothing open to the
     internet; `tailscale serve` also gives HTTPS;
   - a domain name with HTTPS behind a reverse proxy (nginx, Caddy), restricted to their
     own IP addresses;
   - only the home network.
3. **claude.ai or the phone app too?** Those connect from Anthropic's cloud, so the
   server must accept Anthropic's address ranges and OAuth must be on. Say that this
   makes the server reachable from outside their network (still with a code/token
   check), and only do it if they want it.

Then, on the server (back up anything you change):

```bash
mkdir memcairn && cd memcairn
curl -fsSLO https://raw.githubusercontent.com/shanislav/memcairn/main/compose.yml
mkdir -p data && sudo chown 10001 data         # the container runs as uid 10001
echo "MEMCAIRN_PUBLIC_URL=https://memory.example.com" > .env   # the address clients use
# basic search only (see above): echo "MEMCAIRN_EMBED_MODEL=none" >> .env
docker compose up -d
```

The image (`ghcr.io/shanislav/memcairn`, amd64 and arm64) is pulled from GitHub, as the
fixed release named in `compose.yml`. The search model is downloaded on first use into
`data/models`.

- `MEMCAIRN_PUBLIC_URL` must be exactly the address the clients will use (scheme and
  host). It also turns on authentication.
- `compose.yml` publishes the port on `127.0.0.1:8765` only. Put the reverse proxy or
  `tailscale serve` in front of it; [docs/setup.md](docs/setup.md#1-run-the-server) has
  an nginx example with an IP allowlist.
- For claude.ai and the phone: add `MEMCAIRN_OAUTH=1` to `.env` and allow
  Anthropic's outbound ranges in the proxy (see
  [docs/setup.md](docs/setup.md#claudeai-claude-desktop-chat-and-the-mobile-apps-oauth)).
- Check: `curl -s -o /dev/null -w "%{http_code}\n" -X POST https://memory.example.com/mcp`
  prints `401` (not a proxy error such as 403/502, not a timeout).

Give the user the web UI, where they add machines and see what memory holds:

```bash
docker compose exec memcairn memcairn login-link
```

It prints a link that logs in once (15 minutes). The page then asks the user to set a
password: from then on they log in with it, and the browser stays logged in for 30 days
from the last visit. Tell the user to bookmark `https://memory.example.com/ui` and that a
forgotten password is no problem: `login-link` again, then set a new one. Never ask for
the password yourself. Continue with 3C.

## 3C. Connect this computer to a server

You need a pairing code. Either:

- the user opens the web UI (`https://<server>/ui`), Overview → **+ add machine**, types
  a name for this computer and tells you the code; or
- with SSH access to the server: `docker compose exec memcairn memcairn add-machine
  <name>` in the server's Memcairn folder.

Then, on this computer:

```bash
uv tool install memcairn      # no [vector] needed: the server does the searching
memcairn pair https://memory.example.com K7QM-42XD
```

`pair` trades the code for this machine's own token and saves it in
`~/.memcairn/client.toml`, then writes the `memory` MCP entry into `~/.claude.json` and
a session-start hook, both backed up. Every machine gets its own code and name.

**claude.ai and the phone** (only if the server allows Anthropic, see 3B): the user goes
to claude.ai → Settings → Connectors → Add custom connector, URL
`https://memory.example.com/mcp`, and enters a fresh pairing code on the page that opens.
They must do this from a network the server lets in; afterwards the phone app has the
connector too.

## 4. Optional: share auto memory between the user's machines

Only with a server. Ask:

> "Claude Code keeps its own notes on how you like to work (auto memory), separately on
> each computer. Should Memcairn carry them to your other computers too?"

If yes, run in the user's main project folder (where they usually work):

```bash
memcairn setup --dry-run     # show the user what would change
memcairn setup --yes         # after they agree
```

It adds a section to `~/.memcairn/sync.toml` and hooks that run `memcairn sync` at
session start and end. `memcairn unsetup` reverts everything setup and pair changed.

## 5. Optional: bring existing memory along

If step 1 found the official memory server's file (`~/.claude/memory.json` or the path in
its `MEMORY_FILE_PATH`), offer to import it:

- local: `memcairn import server-memory ~/.claude/memory.json`
- server: copy the file there, then
  `docker compose exec memcairn memcairn import server-memory /data/memory.json`

Then remove the old `memory` entry from the configuration (ask first, back up). Two
servers with the same name clash, and the more specific scope wins.

## 6. Check and finish

If this session has Memcairn tools from a claude.ai connector, they stay until the
restart; write the test fact below in the next session, with this machine's own tools.

- `memcairn doctor` checks the configuration, the connection and the hook; it changes
  nothing. Fix what it reports.
- Ask the user to restart Claude Code (or start a new session). The new session begins
  with a short "Long-term memory (Memcairn) is connected" overview; the tools are
  `recall`, `get`, `remember`, `update`, `forget`, `link`, `context`.
- Tell the user that agents can only *ask* to pin a fact (pinned facts reach every
  session); they approve or reject requests at the top of the web UI overview.
- Store one test fact (`remember`, e.g. that Memcairn was installed, and where) and find
  it with `recall`.

Updating later: on the server a backup, `MEMCAIRN_VERSION=X.Y.Z` in `.env`, then
`docker compose pull && docker compose up -d` (details in docs/setup.md); on each machine
`uv tool upgrade memcairn`.

Finally tell the user in two or three sentences what they have: where the memory lives,
how to browse it (`memcairn ui` or `https://<server>/ui`), how to add another machine
(Add machine in the web UI, then `memcairn pair`), and how to undo it
(`memcairn unsetup`, then `uv tool uninstall memcairn`).

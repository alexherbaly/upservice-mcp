# Upservice MCP Server

*[Читать на русском](README.ru.md)*

An MCP (Model Context Protocol) server that wraps the [Upservice Public API](https://public.upservice.io/redoc) so Claude (or any MCP client) can manage employees, projects, sprints, tasks, tags, directories, and channel messages directly.

Built from the OpenAPI spec at `https://public.upservice.io/openapi.json`, verified against the live API.

## What it can do

58 tools covering:

- **Employees** — list employees
- **Projects** — list, create, get, update, delete, set managers/members, mark completed
- **Sprints** — list, create, get, update, delete, complete, activate, add tasks
- **Tags** — list, create, update, delete, assign/unassign to tasks/chats/assets/contacts/attachments
- **Tasks** — list/search, create, get, update, delete, attachments, status changes, effort estimation, worklog, agreement (approval) workflow actions, co-responsibles, agreement/acquaintance sheets
- **Directories** (custom reference catalogs) — list/create/get/update/delete directories and their records, plus bulk relation management (linking records to tasks, projects, orders, etc.)
- **Channels & files** — load/send chat messages, send channel messages, upload files, get file download URLs

Every write tool exposes the well-documented fields explicitly (validated), and most also accept an `extra_fields` dict for advanced/uncommon fields documented in the Upservice API but not modeled individually.

**Mentioning employees:** Upservice only turns `@Name` into a real, notifying mention if it's written as `@[Name](employee_id)`. Tools that write text (`upservice_send_chat_message`, `upservice_send_channel_message`, `upservice_create_task`, `upservice_update_task`) accept a `mentions: [{employee_id, display_name}]` field — put a `{{employee_id}}` placeholder in your text and the server substitutes the correct syntax for you.

**Known API limitation:** `GET /v1/tasks` (used by `upservice_list_tasks`) has no `status`/`is_completed` filter — confirmed against the live API, not just the docs. Filter by `status` client-side after narrowing with `date_end_gte`/`date_end_lte`, `project`, `author`, or `responsible`.

## Setup

Get your Upservice API key first: in your Upservice account, go to account settings → API key. Keys look like `UPS-XXXX-XXXX-XXXX-XXXX`.

### Option A — `uvx` (recommended, no manual install)

Requires [`uv`](https://docs.astral.sh/uv/) installed (`brew install uv` or see their docs). Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "upservice": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/upservice_mcp", "upservice-mcp"],
      "env": {
        "UPSERVICE_API_KEY": "UPS-XXXX-XXXX-XXXX-XXXX"
      }
    }
  }
}
```

If this project is published to a git remote, `--from` can point at it directly instead of a local path:
`"args": ["--from", "git+https://github.com/alexherbaly/upservice-mcp", "upservice-mcp"]` — then a colleague only needs the URL and their own API key, no file copying at all.

Pin to a ref rather than the bare URL — without one, `uvx` tracks the `main` branch, so a bad push to `main` breaks everyone's server on their next run. Two options:

- **`@stable`** (recommended for colleagues) — a tag that CI automatically moves to the tip of `main` every time the build passes. No one has to remember to cut a release for routine fixes; everyone's server just picks up the latest known-good commit next time it restarts.
- **`@v0.2.0`** (a specific version) — frozen forever at that exact commit, for when you want a name you can point back to later (release notes, "the version we tested on date X"). Cut these manually with `git tag` when it's worth a name, not on every push.

Example: `"args": ["--from", "git+https://github.com/alexherbaly/upservice-mcp@stable", "upservice-mcp"]`

`uv` builds and caches an isolated environment on first run; nothing is installed system-wide.

### Option B — plain venv + pip

```bash
cd upservice_mcp
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

```json
{
  "mcpServers": {
    "upservice": {
      "command": "/absolute/path/to/upservice_mcp/venv/bin/python3",
      "args": ["/absolute/path/to/upservice_mcp/src/upservice_mcp/server.py"],
      "env": {
        "UPSERVICE_API_KEY": "UPS-XXXX-XXXX-XXXX-XXXX"
      }
    }
  }
}
```

Restart Claude Desktop afterwards. The tools will appear prefixed with `upservice_`.

### Optional: custom base URL

If Upservice ever changes the API host, or you use a private/on-prem instance, override it:

```json
"env": {
  "UPSERVICE_API_KEY": "UPS-XXXX-XXXX-XXXX-XXXX",
  "UPSERVICE_API_BASE_URL": "https://public.upservice.io"
}
```

## Testing locally

```bash
# uvx
UPSERVICE_API_KEY=UPS-XXXX uvx --from . upservice-mcp

# venv
UPSERVICE_API_KEY=UPS-XXXX venv/bin/python3 src/upservice_mcp/server.py
```

Or test interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uvx --from . upservice-mcp
```

## Notes

- Authentication uses a raw API key in the `Authorization` header (not a `Bearer` token) — this server handles that for you.
- Destructive tools (`delete_*`, `unassign_tag`) are annotated with `destructiveHint: true` so MCP clients can warn users appropriately.
- Some request bodies documented by Upservice have many optional fields; where a tool doesn't model a field explicitly, pass it via `extra_fields` (a plain JSON object) and it will be merged into the request body.
- **Never share your own `UPSERVICE_API_KEY`** — each person should generate their own from their Upservice account settings.

# Chock

A self-hosted approval queue for AI agent teams. Three outcomes: approve, deny, or approve-with-conditions. Agents get structured payloads back.

Stop your agents before they do something irreversible.

## What is this?

Chock is a standalone human-in-the-loop approval service. Your AI agents submit plans for approval via a simple REST API. You see them in a clean web interface. Click Approve, Deny, or set structured conditions. Agents get machine-readable responses back — no parsing natural language, no ambiguity.

The key feature is **contingent approvals**. Not just yes/no — you can approve with conditions. Define a conditions schema upfront (budget limits, scope constraints, time windows), and when you respond conditionally, the agent gets structured data it can actually parse and act on.

No SDK. No vendor lock-in. Pure HTTP. Any agent framework, any model.

**Status: alpha.** Developed and tested internally on sandboxed development machines. If you deploy this: inspect the code, run in a VM or isolated environment, and back up your data before upgrading. This has not been independently security audited. See [SECURITY.md](SECURITY.md) for details.

**Auth note:** If you set `CHOCK_API_KEY`, the server will require the key for all write operations via the API. However, the web UI does not currently send the API key with its requests. This means approving, denying, and submitting conditional responses from the UI will be rejected by the server when a key is set. Agent API calls (which include the key in headers) will work correctly. Web UI auth support is planned.

## Quick Start

```bash
pip install fastapi uvicorn
python server.py
```

Open `http://localhost:8796` in your browser.

### Docker

```bash
docker compose up -d
```

Or build manually:

```bash
docker build -t chock .
docker run -d -p 8796:8796 -v chock-data:/data chock
```

## For AI Agents

Give your agent the contents of [TOOL.md](TOOL.md) as context. It contains:

- REST API reference with all endpoints
- Conditions schema format
- Callback URL support
- OpenAI function-calling tool definitions (works with any compatible framework)
- Usage guidelines (when to request approval, when not to)

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/requests` | Submit an approval request |
| `GET` | `/api/requests` | List requests (filter by `?status=`, `?requester=`) |
| `GET` | `/api/requests/{id}` | Get a single request |
| `GET` | `/api/pending` | List pending requests (sorted by priority) |
| `POST` | `/api/requests/{id}/respond` | Respond: approve, deny, or conditional |
| `DELETE` | `/api/requests/{id}` | Cancel a pending request |
| `GET` | `/api/activity` | Recent activity log |
| `GET` | `/health` | Health check |

## Three Outcomes

| Status | What the agent gets back |
|--------|------------------------|
| **approved** | Green light. Proceed with the plan as submitted. |
| **denied** | Red light. Do not proceed. Check the comment for the reason. |
| **conditional** | Yellow light. Proceed, but read the `conditions` object first and adapt your plan. |

## Contingent Approvals

When submitting a request, agents can define a `conditions_schema` — structured fields the human can fill in:

```json
{
  "conditions_schema": [
    {"key": "budget", "label": "Budget limit ($)", "type": "number", "required": true},
    {"key": "scope", "label": "Scope", "type": "select", "options": ["full", "partial", "minimal"]},
    {"key": "deadline", "label": "Must complete by", "type": "text"}
  ]
}
```

The human sees a form with these fields. When they respond conditionally, the agent gets:

```json
{"conditions": {"budget": "5000", "scope": "partial", "deadline": "Friday 5pm"}}
```

Machine-readable. No parsing. No ambiguity.

## Callback Support

Provide a `callback_url` in your request and Chock will POST the full response when a human decides. No polling needed.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CHOCK_HOST` | `127.0.0.1` | Bind address |
| `CHOCK_PORT` | `8796` | Port number |
| `CHOCK_DB` | `./data/approvals.db` | SQLite database path |
| `CHOCK_WEBHOOKS` | (none) | Comma-separated webhook URLs |
| `CHOCK_API_KEY` | (none) | Optional API key for write operations (reads remain open) |

## Known Limitations

- **Web UI does not support API key auth.** If `CHOCK_API_KEY` is set, the web UI cannot perform write operations (approve, deny, submit conditional responses). API clients that send the key in headers work fine. Fix planned for next release.
- **No built-in HTTPS or rate limiting.** Use a reverse proxy for production deployments. See [SECURITY.md](SECURITY.md).

## Dependencies

- Python 3.10+
- FastAPI
- Uvicorn

## License

MIT

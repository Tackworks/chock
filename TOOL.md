# Chock — Agent Tool Reference

You have access to an approval queue. Use it to request human approval before proceeding with significant actions. These instructions work with any model or agent framework.

## Quick Reference

All endpoints accept and return JSON. Base URL is configurable (default: `http://127.0.0.1:8796`).

### Submit an approval request
```
POST /api/requests
{
  "title": "Migrate database to PostgreSQL",
  "plan": "1. Export current SQLite data\n2. Set up PG schema\n3. Import data\n4. Update connection strings\n5. Run smoke tests",
  "requester": "my-agent",
  "context": "Current SQLite DB hitting concurrency limits under load.",
  "priority": "high",
  "callback_url": "http://my-agent:8080/webhook",
  "conditions_schema": [
    {"key": "downtime_window", "label": "Allowed downtime window", "type": "text", "required": true},
    {"key": "rollback_ok", "label": "Allow automatic rollback?", "type": "boolean", "required": true}
  ]
}
```
Returns `{"id": "chk-abc12345", "status": "pending"}`. Save the ID to poll for the response.

### Check your request
```
GET /api/requests/{request_id}
```

### Poll the pending queue
```
GET /api/pending
```
Returns pending requests sorted by priority (critical first) then age.

### List all requests
```
GET /api/requests
GET /api/requests?status=pending
GET /api/requests?requester=my-agent
```

### Respond to a request (human action)
```
POST /api/requests/{request_id}/respond
{"status": "approved", "responder": "reviewer", "comment": "Go ahead."}
```

```
POST /api/requests/{request_id}/respond
{"status": "denied", "responder": "reviewer", "comment": "Too risky without testing."}
```

```
POST /api/requests/{request_id}/respond
{
  "status": "conditional",
  "responder": "reviewer",
  "comment": "OK but with these constraints.",
  "conditions": {
    "downtime_window": "Saturday 2-4am",
    "rollback_ok": "true"
  }
}
```

### Cancel a pending request
```
DELETE /api/requests/{request_id}?requester=my-agent
```

## Response Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Waiting for human review |
| `approved` | Human approved — proceed with the plan |
| `denied` | Human denied — do not proceed |
| `conditional` | Human approved with conditions — read the conditions payload before proceeding |

## Priorities

| Priority | When to use |
|----------|-------------|
| `critical` | Urgent, time-sensitive approval needed |
| `high` | Important, should be reviewed soon |
| `normal` | Standard approval request |
| `low` | No rush |

## Conditions Schema

When submitting a request, you can provide a `conditions_schema` — a list of structured fields the human can fill in if they choose "conditional" approval. This makes the response machine-readable:

```json
"conditions_schema": [
  {"key": "budget", "label": "Budget limit", "type": "number", "required": true},
  {"key": "scope", "label": "Scope restriction", "type": "select", "options": ["full", "partial", "minimal"]},
  {"key": "notes", "label": "Additional notes", "type": "text"}
]
```

Field types: `text`, `number`, `boolean`, `select`.

When the human responds conditionally, you get back:
```json
"conditions": {"budget": "5000", "scope": "partial", "notes": "Start with one table"}
```

Parse these conditions and adapt your plan accordingly.

## Callback URL

If you provide a `callback_url` in your request, Chock will POST the response to that URL when a human responds. The payload:

```json
{
  "request_id": "chk-abc12345",
  "result": {
    "status": "conditional",
    "responder": "reviewer",
    "comment": "OK with constraints.",
    "conditions": {"budget": "5000"},
    "responded_at": "2026-04-07T..."
  },
  "timestamp": "..."
}
```

This lets you avoid polling. If you don't provide a callback, poll `GET /api/requests/{id}` until the status changes from `pending`.

## When to Request Approval

- Before making irreversible changes (database migrations, file deletions, infrastructure changes)
- Before spending resources (API calls, compute, purchases)
- Before external actions (sending messages, posting to services, creating accounts)
- When the plan involves multiple steps and the human should review the full plan first

## When NOT to Request Approval

- Routine operations the human has pre-approved
- Read-only actions (querying data, checking status)
- Internal reasoning or planning (only request when you have a plan to present)

## OpenAI Function-Calling Tool Definitions

```json
[
  {
    "type": "function",
    "function": {
      "name": "chock_request",
      "description": "Submit an approval request. Returns a request ID for polling.",
      "parameters": {
        "type": "object",
        "properties": {
          "title": {"type": "string", "description": "Short title for the approval request"},
          "plan": {"type": "string", "description": "The plan or action to be approved"},
          "requester": {"type": "string", "description": "Your agent name"},
          "context": {"type": "string", "description": "Background info for the reviewer"},
          "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
          "callback_url": {"type": "string", "description": "URL to POST the response to (optional)"},
          "tags": {"type": "array", "items": {"type": "string"}},
          "conditions_schema": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "key": {"type": "string"},
                "label": {"type": "string"},
                "type": {"type": "string", "enum": ["text", "number", "boolean", "select"]},
                "options": {"type": "array", "items": {"type": "string"}},
                "required": {"type": "boolean"},
                "description": {"type": "string"}
              },
              "required": ["key", "label"]
            },
            "description": "Structured fields the human can fill in for conditional approval"
          }
        },
        "required": ["title"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "chock_check",
      "description": "Check the status of an approval request. Returns the full request including response if decided.",
      "parameters": {
        "type": "object",
        "properties": {
          "request_id": {"type": "string", "description": "The request ID (chk-...)"}
        },
        "required": ["request_id"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "chock_pending",
      "description": "List all pending approval requests, sorted by priority then age.",
      "parameters": {
        "type": "object",
        "properties": {}
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "chock_cancel",
      "description": "Cancel a pending approval request.",
      "parameters": {
        "type": "object",
        "properties": {
          "request_id": {"type": "string"},
          "requester": {"type": "string"}
        },
        "required": ["request_id"]
      }
    }
  }
]
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHOCK_HOST` | `127.0.0.1` | Bind address |
| `CHOCK_PORT` | `8796` | Port number |
| `CHOCK_DB` | `./data/approvals.db` | SQLite database path |
| `CHOCK_WEBHOOKS` | (none) | Comma-separated webhook URLs for event notifications |

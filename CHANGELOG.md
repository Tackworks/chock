# Changelog

## [1.1.0] - 2026-04-08

### Added
- **Dark/Light theme toggle** — Click the moon/sun icon in the header. Preference persists via localStorage.
- **Request search/filter** — Real-time search across title, plan, requester, context, and tags. Press `/` to focus, `Escape` to clear.
- **Server-side search** — `GET /api/requests?q=term&tag=name&priority=high` for API consumers.

## [1.0.0] - 2026-04-08

Initial public release.

- Approval queue with three outcomes: approve, deny, conditional
- Contingent approvals with structured conditions schema
- Machine-readable condition responses (no freeform parsing)
- Callback URL support for push notification on decisions
- Priority levels for request ordering
- Activity log
- Web UI for reviewing and responding to requests
- SQLite backend, single-file server
- Docker support

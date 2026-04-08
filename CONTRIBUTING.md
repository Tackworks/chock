# Contributing to Chock

Thanks for considering a contribution. Here's how to get involved.

## Quick Setup

```bash
git clone https://github.com/Tackworks/chock.git
cd chock
pip install fastapi uvicorn
python server.py
```

Open `http://localhost:8796` and you're running.

## How to Contribute

1. **Check existing issues** before opening a new one.
2. **Fork and branch.** Create a feature branch from `main`.
3. **Keep it small.** One feature or fix per PR. Easier to review, easier to merge.
4. **Test your changes.** Start the server, click around, hit the API. Make sure nothing broke.
5. **Open a PR** with a clear description of what changed and why.

## What We're Looking For

- Bug fixes
- New condition field types
- UI improvements
- Documentation improvements
- Integration guides for agent frameworks

## What We're NOT Looking For

- External dependencies beyond FastAPI + Uvicorn
- Database migrations away from SQLite
- Authentication systems (use a reverse proxy)
- Breaking API changes

## Code Style

- Single file (`server.py`) is intentional. Don't split it.
- Standard library over third-party when possible.
- Keep it readable.

## Questions?

Open an issue or email tackworks@proton.me.

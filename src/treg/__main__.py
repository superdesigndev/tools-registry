"""`python -m treg` — run the server, or `python -m treg keygen` for a Fernet key."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "keygen":
        from .crypto import new_key

        print(new_key())
        return

    import uvicorn

    # Honor $PORT (Render/Heroku set it and route/health-check that port) — a hard-coded port makes
    # the deploy unreachable. Falls back to the local dev port.
    port = int(os.environ.get("PORT", "18790"))
    uvicorn.run("treg.api:app", host="0.0.0.0", port=port, reload="--reload" in sys.argv)


if __name__ == "__main__":
    main()

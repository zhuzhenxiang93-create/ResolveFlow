"""Single-process public host: the built Vue Demo at `/` and the isolated Demo API at `/api/python`.

The browser build already calls `/api/python/...` (same path the Vite dev proxy uses), so one
container can serve both without CORS. Never imports the production `api.main` app.
"""
import os
from pathlib import Path

from starlette.staticfiles import StaticFiles

from api.portfolio_demo import create_app

PREFIX = "/api/python"


def create_site():
    api = create_app()
    dist = Path(os.getenv("RESOLVEFLOW_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "ResolveFlowFrontend" / "dist"))
    if not (dist / "index.html").is_file():
        raise RuntimeError(f"Built frontend not found at {dist}; run `VITE_DEMO_MODE=true npm run build` first")
    static = StaticFiles(directory=str(dist), html=True)

    async def site(scope, receive, send):
        if scope["type"] == "lifespan":
            return await api(scope, receive, send)
        path = scope.get("path", "")
        if path == PREFIX or path.startswith(PREFIX + "/"):
            inner = dict(scope)
            inner["path"] = path[len(PREFIX):] or "/"
            inner["raw_path"] = inner["path"].encode()
            return await api(inner, receive, send)
        return await static(scope, receive, send)

    return site

"""TradeLab — private multi-account intraday automation for Kotak Neo."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .config import get_settings
from .db import init_db
from .routers import accounts, alerts, auth, automation, basket, holidays, reports, sections, stocks
from .services import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    accounts.remove_practice_accounts()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="TradeLab", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

for r in (auth.router, accounts.router, stocks.router, basket.router, automation.router, holidays.router,
          reports.router, alerts.router, sections.router):
    app.include_router(r)


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError):
    """422 with where and why, but never what was sent back: it may hold an MPIN or a TOTP secret, or a
    value that can't even be written as JSON (Infinity)."""
    errors = [{"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg") or "Invalid value"),
               "type": str(e.get("type") or "")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.middleware("http")
async def private_headers(request: Request, call_next):
    response = await call_next(request)
    # Private app: keep it out of search engines and other sites' frames.
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(rest: str):
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """Serve the installable web app (PWA) and fall back to index.html for client routes."""
    dist = get_settings().frontend_dist.resolve()
    if path:
        candidate = (dist / path).resolve()
        if candidate.is_file() and dist in candidate.parents:
            headers = {}
            if path.startswith("assets/"):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif path in ("sw.js", "registerSW.js", "manifest.webmanifest"):
                headers["Cache-Control"] = "no-cache"
            return FileResponse(candidate, headers=headers)
    index = dist / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return PlainTextResponse("Frontend not built. Run `npm run build` in frontend/.", status_code=503)

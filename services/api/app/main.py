import logging
import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.db import engine
from app.errors import DomainError
from app.routers import router
from app.routers.ops import router as ops_router
from app.routers.replays import router as replays_router
from app.transitions import TransitionError

logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(title="RepairFlow API", version="0.1.0")
app.include_router(router)
app.include_router(ops_router)
app.include_router(replays_router)
STATIC = pathlib.Path(__file__).parent / "static"


@app.exception_handler(DomainError)
async def domain_error(_: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail, **exc.extra})


@app.exception_handler(TransitionError)
async def transition_error(_: Request, exc: TransitionError):
    return JSONResponse(status_code=409, content={"error": "illegal_transition", "detail": str(exc)})


@app.get("/healthz")
def healthz():
    with engine().connect() as c:
        c.execute(text("select 1"))
    return {"ok": True}


@app.get("/review", include_in_schema=False)
def review_page():
    return FileResponse(STATIC / "review.html")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app/")


class DashboardFiles(StaticFiles):
    """index.html must be revalidated so a redeploy is picked up; hashed assets never change."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        immutable = path.startswith("assets/")
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


# React dashboard (built in the image). /review stays as the minimal fallback page.
if (STATIC / "dashboard").is_dir():
    app.mount("/app", DashboardFiles(directory=STATIC / "dashboard", html=True), name="dashboard")

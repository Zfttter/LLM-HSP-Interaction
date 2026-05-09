from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.routers import pages, api
from app.routers import admin

app = FastAPI(title="HSP-LLM Experiment Platform", docs_url=None, redoc_url=None)

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, max_age=86400)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(pages.router)
app.include_router(api.router, prefix="/api")
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok"}

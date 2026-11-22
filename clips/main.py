"""Serve the editor and its local HTTP API from one origin."""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import Conflict, Store
from .text import Edit

ROOT = Path(__file__).resolve().parents[1]


class Replacement(BaseModel):
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=0)
    text: str


class ClipPatch(BaseModel):
    base_revision: str
    title: str | None = Field(default=None, max_length=120)
    edits: list[Replacement] = Field(default_factory=list)


def create_app(database: Path | None = None) -> FastAPI:
    store = Store(database or Path(os.environ.get("TTS_CLIPS_DB", ROOT / "data" / "clips.sqlite3")))
    app = FastAPI(title="TTS Clips")

    @app.get("/api/clips")
    def list_clips():
        return store.list_clips()

    @app.post("/api/clips", status_code=201)
    def create_clip():
        return store.create()

    @app.get("/api/clips/{clip_id}")
    def get_clip(clip_id: str):
        try:
            return store.get(clip_id)
        except KeyError:
            raise HTTPException(404, "Клип не найден.") from None

    @app.patch("/api/clips/{clip_id}")
    def update_clip(clip_id: str, body: ClipPatch):
        try:
            return store.update(clip_id, body.base_revision, body.title, [
                Edit(edit.start, edit.end, edit.text) for edit in body.edits
            ])
        except Conflict as exc:
            return JSONResponse({"current": exc.current}, status_code=409)
        except KeyError:
            raise HTTPException(404, "Клип не найден.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    dist = ROOT / "dist"
    app.mount("/assets", StaticFiles(directory=dist / "assets", check_dir=False), name="assets")

    @app.get("/", include_in_schema=False)
    def editor():
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(503, "Соберите интерфейс командой npm run build.")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app

"""HTTP commands for export planning, publication and playback."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .exports import Blocked
from .store import Conflict


class ExportOptions(BaseModel):
    revision: str
    mode: Literal["draft", "final"] = "final"
    format: Literal["wav", "mp3"] = "wav"
    leading_ms: int = Field(default=0, ge=0, le=10000, strict=True)
    gap_ms: int = Field(default=150, ge=0, le=10000, strict=True)


class ExportRequest(ExportOptions):
    plan_id: str


def export_router(exports):
    router = APIRouter(prefix="/api")

    @router.post("/clips/{clip_id}/export-plan")
    def plan(clip_id: str, options: ExportOptions):
        try:
            return exports.plan(clip_id, options.model_dump())
        except Conflict as error:
            return JSONResponse({"current": error.current}, status_code=409)
        except KeyError:
            raise HTTPException(404, "Клип не найден.") from None

    @router.post("/clips/{clip_id}/exports", status_code=201)
    def assemble(clip_id: str, request: ExportRequest):
        try:
            return exports.assemble(clip_id, request.model_dump(exclude={"plan_id"}), request.plan_id)
        except Conflict as error:
            return JSONResponse({"current": error.current}, status_code=409)
        except Blocked as error:
            return JSONResponse({"plan": error.plan}, status_code=409)
        except KeyError:
            raise HTTPException(404, "Клип не найден.") from None
        except OSError as error:
            raise HTTPException(500, "Не удалось записать файл сборки.") from error

    @router.get("/clips/{clip_id}/exports")
    def previous(clip_id: str):
        try:
            return exports.list(clip_id)
        except KeyError:
            raise HTTPException(404, "Клип не найден.") from None

    @router.get("/exports/{export_id}/{format}")
    def file(export_id: str, format: Literal["wav", "mp3"], download: bool = False):
        try:
            path = exports.file(export_id, format)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "Файл экспорта не найден.") from None
        except (ValueError, OSError):
            raise HTTPException(409, "Файл экспорта повреждён или недоступен.") from None
        return FileResponse(path, media_type="audio/wav" if format == "wav" else "audio/mpeg",
                            filename=f"tts-clips-{export_id[:8]}.{format}" if download else None)

    return router

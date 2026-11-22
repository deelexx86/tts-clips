"""HTTP commands for voice creation and playback."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .audio import MAX_WAV
from .store import Conflict


class Design(BaseModel):
    text: str = Field(max_length=240)
    description: str = Field(max_length=1000)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


class Pin(BaseModel):
    preview_id: str
    name: str = Field(max_length=120)


class SegmentRequest(BaseModel):
    revision: str
    segment_id: str


def required(text, message):
    if not text.strip():
        raise HTTPException(422, message)
    return text.strip()


def voice_router(jobs):
    router = APIRouter(prefix="/api")

    @router.get("/voices")
    def voices():
        return jobs.voices()

    @router.get("/previews")
    def previews():
        return jobs.list()

    @router.post("/previews/design", status_code=202)
    def design(body: Design):
        phrase = required(body.text, "Введите текст пробы.")
        description = required(body.description, "Опишите голос.")
        if "(" in description or ")" in description:
            raise HTTPException(422, "Уберите круглые скобки из описания голоса.")
        return jobs.preview("design", phrase, description, body.seed)

    @router.post("/previews/clone", status_code=202)
    async def clone(request: Request, text: str = Query(max_length=240),
                    seed: int = Query(default=42, ge=0, le=2**31 - 1)):
        phrase = required(text, "Введите текст пробы.")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_WAV:
                raise HTTPException(413, "Референс превышает 16 МиБ.")
            data.extend(chunk)
        try:
            return await run_in_threadpool(jobs.preview, "clone", phrase, "", seed, bytes(data))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/voices", status_code=201)
    def pin(body: Pin):
        try:
            return jobs.pin(body.preview_id, required(body.name, "Введите название голоса."))
        except KeyError:
            raise HTTPException(404, "Проба не найдена.") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/clips/{clip_id}/audio-jobs")
    def segment_jobs(clip_id: str):
        return jobs.list(clip_id)

    @router.post("/clips/{clip_id}/audio-jobs", status_code=202)
    def synthesize(clip_id: str, body: SegmentRequest):
        try:
            return jobs.segment(clip_id, body.revision, body.segment_id)
        except KeyError:
            raise HTTPException(404, "Клип, сегмент или голос не найден.") from None
        except Conflict as error:
            return JSONResponse({"current": error.current}, status_code=409)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/audio-jobs/{job_id}")
    def job(job_id: str):
        try:
            return jobs.get(job_id)
        except KeyError:
            raise HTTPException(404, "Задание не найдено.") from None

    @router.post("/audio-jobs/{job_id}/cancel")
    def cancel(job_id: str):
        try:
            return jobs.cancel(job_id)
        except KeyError:
            raise HTTPException(404, "Задание не найдено.") from None

    @router.get("/audio/{key}")
    def audio(key: str):
        try:
            return FileResponse(jobs.assets.path(key), media_type="audio/wav")
        except KeyError:
            raise HTTPException(404, "WAV не найден.") from None

    return router

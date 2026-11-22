"""HTTP commands for voice creation and playback."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .audio import MAX_WAV
from .speech_ready import PreparationRequired
from .store import Conflict
from .takes import CommandConflict


class Design(BaseModel):
    text: str = Field(max_length=240)
    description: str = Field(max_length=1000)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


class Pin(BaseModel):
    preview_id: str
    name: str = Field(max_length=120)


class ClipSynthesis(BaseModel):
    revision: str
    key: str = Field(min_length=1, max_length=128)
    speech_confirmation: str | None = Field(default=None, max_length=64)

    def command(self):
        # Keep command serialization compatible with existing idempotency keys.
        return self.model_dump(exclude={"speech_confirmation"} if self.speech_confirmation is None else set())


class GenerationRequest(ClipSynthesis):
    mode: Literal["selected", "missing", "repeat", "variant"]
    segments: list[str] = Field(default_factory=list)
    source_take: str | None = None


class SelectionRequest(BaseModel):
    take_id: str
    version: int = Field(ge=0)


class RecoveryRequest(ClipSynthesis):
    source_take: str | None = None
    include_suspect: bool = False


class ReviewRequest(BaseModel):
    listened: bool | None = None
    decision: Literal["pending", "accepted", "rejected"] | None = None
    note: str | None = Field(default=None, max_length=500)


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

    @router.get("/clips/{clip_id}/takes")
    def takes(clip_id: str):
        try:
            return jobs.takes.workspace(clip_id)
        except KeyError:
            raise HTTPException(404, "Клип не найден.") from None

    @router.post("/clips/{clip_id}/generations", status_code=202)
    def synthesize(clip_id: str, body: GenerationRequest):
        try:
            return jobs.takes.generate(clip_id, body.command())
        except PreparationRequired as error:
            raise HTTPException(422, error.detail) from error
        except KeyError:
            raise HTTPException(404, "Клип или дубль не найден.") from None
        except Conflict as error:
            return JSONResponse({"current": error.current}, status_code=409)
        except CommandConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/generations/{generation_id}/cancel")
    def cancel_generation(generation_id: str):
        try:
            return jobs.takes.cancel(generation_id)
        except KeyError:
            raise HTTPException(404, "Генерация не найдена.") from None

    @router.put("/clips/{clip_id}/take-selection")
    def select(clip_id: str, body: SelectionRequest):
        try:
            return jobs.takes.select(clip_id, body.take_id, body.version)
        except KeyError:
            raise HTTPException(404, "Дубль или WAV не найден.") from None
        except CommandConflict as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/clips/{clip_id}/recoveries", status_code=202)
    def recover(clip_id: str, body: RecoveryRequest):
        try:
            return jobs.recovery.begin(clip_id, body.command())
        except PreparationRequired as error:
            raise HTTPException(422, error.detail) from error
        except KeyError:
            raise HTTPException(404, "Клип или дубль не найден.") from None
        except Conflict as error:
            return JSONResponse({"current": error.current}, status_code=409)
        except CommandConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/recoveries/{run_id}/cancel")
    def cancel_recovery(run_id: str):
        try:
            return jobs.recovery.cancel(run_id)
        except KeyError:
            raise HTTPException(404, "Восстановление не найдено.") from None

    @router.patch("/clips/{clip_id}/takes/{take_id}/review")
    def review(clip_id: str, take_id: str, body: ReviewRequest):
        try:
            return jobs.takes.review(clip_id, take_id, body.model_dump(exclude_none=True))
        except KeyError:
            raise HTTPException(404, "Дубль не найден.") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/clips/{clip_id}/takes/{take_id}/qc")
    def qc_history(clip_id: str, take_id: str):
        try:
            return jobs.qc.history(clip_id, take_id)
        except KeyError:
            raise HTTPException(404, "Дубль не найден.") from None

    @router.post("/clips/{clip_id}/takes/{take_id}/qc", status_code=202)
    def recheck(clip_id: str, take_id: str):
        try:
            return jobs.qc.submit(clip_id, take_id)
        except KeyError:
            raise HTTPException(404, "Дубль не найден.") from None

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

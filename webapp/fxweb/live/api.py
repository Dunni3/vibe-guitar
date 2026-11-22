"""HTTP routes for the Live page and recordings.

    GET    /api/live                    source, recording, disk status
    GET    /api/live/sources            what can be listened to here
    POST   /api/live/source             {"kind": "sim"|"tap"|"alsa", ...}
    POST   /api/live/pause              stop listening (no reads, analysis or history); remembered
    POST   /api/live/resume
    GET    /api/live/stream?fps=30      Server-Sent Events: one JSON analysis frame per event
    POST   /api/live/record/start       {"name"?, "notes"?}
    POST   /api/live/record/stop
    POST   /api/live/record/mark        drop a marker at the current time in the take
    POST   /api/live/record/retro       {"seconds": 30, "name"?, "notes"?}: save what you just played
    GET    /api/recordings
    GET    /api/recordings/{id}
    PATCH  /api/recordings/{id}         {"name"?, "notes"?}
    DELETE /api/recordings/{id}
    GET    /api/recordings/{id}/audio          the WAV, all channels (download)
    GET    /api/recordings/{id}/audio?ch=N     one channel, mono (for the browser's player)

SSE rather than a WebSocket: the stream only goes one way, and it needs no
extra dependency on the Pi, which is often offline.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .hub import LiveError, LiveHub
from .sources import available_sources


def add_live_routes(app: FastAPI, hub: LiveHub, body_json) -> None:
    store = hub.store

    def obj(body: Any) -> dict:
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise HTTPException(422, "body must be a JSON object")
        return body

    def text_field(body: dict, key: str, limit: int) -> str | None:
        v = body.get(key)
        if v is None:
            return None
        if not isinstance(v, str) or len(v) > limit:
            raise HTTPException(422, f"'{key}' must be a string of at most {limit} characters")
        return v

    async def optional_body(request: Request) -> dict:
        raw = await request.body()
        return obj(await body_json(request)) if raw.strip() else {}

    def conflict(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except LiveError as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/live")
    async def live_status():
        return hub.status()

    @app.get("/api/live/sources")
    async def live_sources():
        return {"sources": available_sources(hub.tap_path), "current": hub.source_status()}

    @app.post("/api/live/source")
    async def live_source(request: Request):
        body = obj(await body_json(request))
        try:
            await asyncio.to_thread(hub.start, body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        except LiveError as e:
            raise HTTPException(409, str(e)) from None
        return hub.status()

    @app.post("/api/live/pause")
    async def live_pause():
        await asyncio.to_thread(conflict, hub.pause)
        return hub.status()

    @app.post("/api/live/resume")
    async def live_resume():
        await asyncio.to_thread(hub.resume)
        return hub.status()

    @app.get("/api/live/stream")
    async def live_stream(request: Request, fps: float = 30, limit: int = 0):
        if not 1 <= fps <= 60:
            raise HTTPException(422, "fps must be 1-60")
        sub = hub.subscribe(asyncio.get_running_loop(), fps)

        async def events():
            sent = 0
            try:
                yield "retry: 1000\n\n"
                while True:
                    try:
                        text = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {text}\n\n"
                    sent += 1
                    if limit and sent >= limit:
                        break
            finally:
                hub.unsubscribe(sub)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/live/record/start")
    async def record_start(request: Request):
        body = await optional_body(request)
        return conflict(hub.start_recording, text_field(body, "name", 120), text_field(body, "notes", 4000))

    @app.post("/api/live/record/stop")
    async def record_stop():
        try:
            return await asyncio.to_thread(hub.stop_recording)
        except LiveError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/live/record/mark")
    async def record_mark():
        return {"t": conflict(hub.mark)}

    @app.post("/api/live/record/retro")
    async def record_retro(request: Request):
        body = await optional_body(request)
        seconds = body.get("seconds", 30)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 3600:
            raise HTTPException(422, "'seconds' must be a number from 1 to 3600")
        name, notes = text_field(body, "name", 120), text_field(body, "notes", 4000)
        try:
            return await asyncio.to_thread(hub.save_retro, float(seconds), name, notes)
        except LiveError as e:
            raise HTTPException(409, str(e)) from None

    def meta_or_404(rid: str) -> dict:
        try:
            return store.get(rid)
        except KeyError:
            raise HTTPException(404, f"no recording '{rid}'") from None

    @app.get("/api/recordings")
    async def recordings():
        return {"recordings": await asyncio.to_thread(store.list), "disk": store.disk()}

    @app.get("/api/recordings/{rid}")
    async def recording(rid: str):
        return meta_or_404(rid)

    @app.patch("/api/recordings/{rid}")
    async def recording_update(rid: str, request: Request):
        meta_or_404(rid)
        body = obj(await body_json(request))
        return store.update(rid, text_field(body, "name", 120), text_field(body, "notes", 4000))

    @app.delete("/api/recordings/{rid}")
    async def recording_delete(rid: str):
        meta_or_404(rid)
        try:
            store.delete(rid)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {"deleted": rid}

    @app.get("/api/recordings/{rid}/audio")
    async def recording_audio(rid: str, ch: int | None = None):
        m = meta_or_404(rid)
        if m.get("status") == "recording":
            raise HTTPException(409, "still recording")
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in m.get("name", rid)).strip() or rid
        if ch is None:
            return FileResponse(store.wav(rid), media_type="audio/wav", filename=f"{safe}.wav")
        try:
            path = await asyncio.to_thread(store.channel_wav, rid, ch)
        except KeyError:
            raise HTTPException(404, f"no channel {ch}") from None
        return FileResponse(path, media_type="audio/wav",
                            headers={"Content-Disposition": f'inline; filename="{rid}.ch{ch}.wav"'})

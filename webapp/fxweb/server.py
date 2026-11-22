"""HTTP + JSON API and static UI for the FX box.

    GET    /api/catalog              effects, params, ranges, defaults (from fxrender)
    GET    /api/presets              list (invalid files are listed with their error)
    GET    /api/presets/{id}         one preset
    POST   /api/presets              save-as: body {"preset": {...}}; returns the new id
    PUT    /api/presets/{id}         save over an existing preset; reloads it if it's running
    DELETE /api/presets/{id}
    GET    /api/engine               what's loaded
    POST   /api/engine/load          {"preset_id": "x"} or {"preset": {...}, "preset_id": "x"?}
                                     (the second form auditions unsaved edits)
    GET    /api/health               buffer, latency, DSP load, xruns, CPU temp
    /api/live/..., /api/recordings/...   live analysis + recording (fxweb/live/api.py)

Control latency doesn't matter here, so it's plain request/response; the UI
polls /api/health every couple of seconds while it's visible. The one stream
is the Live page's analysis frames (Server-Sent Events).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .catalog import REPO_ROOT, CatalogError, cpp_check_text, find_fxrender, load_catalog
from .engine import Engine, LoadedPreset, make_engine
from .live.api import add_live_routes
from .live.hub import LiveHub
from .live.recordings import RecordingStore
from .live.sources import TapSource
from .live.tap import default_tap_path
from .presets import PresetError, PresetStore, parse_json

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PRESETS = REPO_ROOT / "presets"
DEFAULT_RECORDINGS = REPO_ROOT / "recordings"
INITIAL_PRESET = "clean"


def _preset_info(lp: LoadedPreset | None) -> dict | None:
    """What was loaded when a take was recorded, saved in its metadata."""
    if lp is None:
        return None
    return _loaded_dict(lp) | {"preset": lp.preset}


def _loaded_dict(lp: LoadedPreset | None) -> dict | None:
    if lp is None:
        return None
    return {"id": lp.id, "name": lp.name, "buffer_size": lp.buffer_size, "modified": lp.modified}


def create_app(
    store: PresetStore, engine: Engine, initial_preset: str | None = INITIAL_PRESET,
    live: LiveHub | None = None,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if live is not None:
            live.close()  # finishes a take in progress so its file is complete

    app = FastAPI(title="Guitar FX Box", docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.store = store
    app.state.engine = engine
    app.state.live = live

    def load(preset: dict, preset_id: str | None, modified: bool = False) -> LoadedPreset:
        lp = engine.load(preset, preset_id, modified=modified)
        if live is not None:
            live.set_preset(lp.preset)
        return lp

    if live is not None:
        live.preset_info = lambda: _preset_info(engine.loaded())

    if initial_preset and store.exists(initial_preset):
        try:
            load(store.get(initial_preset), initial_preset)
        except PresetError:
            pass

    @app.middleware("http")
    async def no_stale_ui(request: Request, call_next):
        # Phones cache aggressively; make them revalidate so a `git pull` on the box shows up.
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.exception_handler(PresetError)
    async def preset_error(_: Request, e: PresetError):
        return JSONResponse(status_code=422, content={"detail": str(e)})

    async def body_json(request: Request) -> Any:
        # Parse ourselves: strict (no NaN) and no type coercion, like the C++ side.
        try:
            return parse_json((await request.body()).decode("utf-8-sig"))
        except UnicodeDecodeError:
            raise PresetError("request body must be UTF-8 JSON") from None

    def get_or_404(preset_id: str) -> dict:
        try:
            return store.get(preset_id)
        except KeyError:
            raise HTTPException(404, f"no preset '{preset_id}'") from None

    def preset_from(body: Any) -> dict:
        if not isinstance(body, dict) or "preset" not in body:
            raise PresetError("body must be {\"preset\": {...}}")
        return body["preset"]

    @app.get("/api/catalog")
    async def catalog():
        return store.catalog

    @app.get("/api/presets")
    async def list_presets():
        return {"presets": store.list(), "loaded": _loaded_dict(engine.loaded())}

    @app.get("/api/presets/{preset_id}")
    async def get_preset(preset_id: str):
        return {"id": preset_id, "preset": get_or_404(preset_id)}

    @app.post("/api/presets", status_code=201)
    async def create_preset(request: Request):
        preset = store.parse_obj(preset_from(await body_json(request)))
        new_id = store.new_id(preset["name"])
        saved = store.save(new_id, preset)
        return {"id": new_id, "preset": saved}

    @app.put("/api/presets/{preset_id}")
    async def save_preset(preset_id: str, request: Request):
        get_or_404(preset_id)  # PUT only overwrites; use POST for save-as
        saved = store.save(preset_id, preset_from(await body_json(request)))
        lp = engine.loaded()
        reloaded = lp is not None and lp.id == preset_id
        if reloaded:
            load(saved, preset_id)
        return {"id": preset_id, "preset": saved, "reloaded": reloaded}

    @app.delete("/api/presets/{preset_id}")
    async def delete_preset(preset_id: str):
        try:
            store.delete(preset_id)
        except KeyError:
            raise HTTPException(404, f"no preset '{preset_id}'") from None
        return {"deleted": preset_id}

    @app.get("/api/engine")
    async def engine_status():
        return {"engine": engine.name, "loaded": _loaded_dict(engine.loaded())}

    @app.post("/api/engine/load")
    async def engine_load(request: Request):
        body = await body_json(request)
        if not isinstance(body, dict):
            raise PresetError("body must be a JSON object")
        preset_id = body.get("preset_id")
        if preset_id is not None and not isinstance(preset_id, str):
            raise PresetError("'preset_id' must be a string")
        if "preset" in body:
            lp = load(store.parse_obj(body["preset"]), preset_id, modified=True)
        elif preset_id:
            lp = load(get_or_404(preset_id), preset_id)
        else:
            raise PresetError("give 'preset_id' or 'preset'")
        return {"engine": engine.name, "loaded": _loaded_dict(lp)}

    @app.get("/api/health")
    async def health():
        return engine.health().to_dict()

    if live is not None:
        add_live_routes(app, live, body_json)

    explainers = REPO_ROOT / "explainers"
    if explainers.is_dir():  # the concept pages, readable on the phone with no internet
        app.mount("/explainers", StaticFiles(directory=explainers, html=True), name="explainers")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


def pick_live_source(requested: str, tap_path: Path) -> str:
    """'auto': the engine's tap if something is writing to it, else the simulation."""
    if requested != "auto":
        return requested
    if tap_path.exists():
        src = TapSource(tap_path)
        try:
            src.open()
            if src.read(timeout=0.3) is not None:
                return f"tap:{tap_path}"
        except Exception:
            pass
        finally:
            src.close()
    return "sim"


def build_default_app(
    presets_dir: Path = DEFAULT_PRESETS, fxrender: str | None = None, engine: str = "fake",
    live_source: str | None = "auto", recordings_dir: Path = DEFAULT_RECORDINGS,
    tap_path: Path | None = None, retro_seconds: float = 90.0, mains_hz: float = 60.0,
) -> FastAPI:
    exe = find_fxrender(fxrender)
    store = PresetStore(
        presets_dir, load_catalog(exe), cpp_check=functools.partial(cpp_check_text, exe)
    )
    hub = None
    if live_source:
        tap = tap_path or default_tap_path()
        hub = LiveHub(RecordingStore(recordings_dir), fxrender=exe, tap_path=tap,
                      retro_seconds=retro_seconds, mains_hz=mains_hz)
        hub.start(pick_live_source(live_source, tap))
    return create_app(store, make_engine(engine), live=hub)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Guitar FX box web app")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    ap.add_argument("--presets", type=Path, default=DEFAULT_PRESETS, help="preset directory")
    ap.add_argument("--fxrender", help="path to fxrender (default build/fxrender, or $FX_FXRENDER)")
    ap.add_argument("--engine", default="fake", help="engine backend (default: fake)")
    ap.add_argument("--live-source", default="auto",
                    help="live signal: auto (engine tap if running, else sim), sim, tap[:PATH], "
                         "alsa:DEVICE[:CHANNELS], or none to turn the Live page off")
    ap.add_argument("--tap", type=Path, help=f"engine tap file (default {default_tap_path()})")
    ap.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS, help="where takes are saved")
    ap.add_argument("--retro-seconds", type=float, default=90.0,
                    help="how much of the live signal to keep in memory for 'save last N s' (default 90)")
    ap.add_argument("--mains-hz", type=float, default=60.0, choices=[50.0, 60.0],
                    help="mains frequency for the hum meter (default 60)")
    args = ap.parse_args(argv)

    try:
        app = build_default_app(
            args.presets, args.fxrender, args.engine,
            live_source=None if args.live_source == "none" else args.live_source,
            recordings_dir=args.recordings, tap_path=args.tap, retro_seconds=args.retro_seconds,
            mains_hz=args.mains_hz)
    except (CatalogError, ValueError) as e:
        raise SystemExit(f"fx-web: {e}") from None

    import uvicorn

    live = app.state.live
    src = live.source_status()["spec"].get("kind", "?") if live else "off"
    print(f"fx-web: serving on http://{args.host}:{args.port}  (engine: {args.engine}, live: {src})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

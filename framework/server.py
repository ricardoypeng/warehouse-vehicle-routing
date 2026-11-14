"""HTTP adapter with independent per-world locks and atomic stepping."""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from _thread import LockType
from typing import Any, cast
import json
import shutil
import threading
import uuid
from urllib.parse import urlsplit

from config.execution import WORLD_TIMEOUT_SECONDS
from config.resources import MAX_SESSIONS, MAX_STEP_TICKS, MAX_REQUEST_BYTES
from config.frontend import SETTINGS as FRONTEND_SETTINGS
from config.scenario import NUMERIC_BOUNDS

from .backend.config import Config
from .backend.cpu.engine import Engine
from .registry import Registry
from .process_engine import ProcessEngine
from .runtime import ROOT, run_directory
from .runner import save_json



@dataclass
class Session:
    """
    Group mutable state and synchronization for one HTTP simulation world.

    Attributes:
        engine (Engine | ProcessEngine): Independent kernel or supervised worker.
        folder (Path): Output directory beneath project tmp.
        lock   (Lock): Serializes snapshot reads and tick batches for this world.
        failed (str | None): Recorded runtime failure; prevents further stepping.
    """

    engine: Engine | ProcessEngine
    folder: Path
    lock: LockType = field(default_factory=threading.Lock)
    failed: str | None = None


class Server(ThreadingHTTPServer):
    """
    Serve frontend assets and independent simulation sessions concurrently.

    The sessions lock protects dictionary membership. Each session has its own
    lock so requests for one world do not serialize unrelated worlds. Simulation
    time advances only through explicit step requests, not a background timer.
    """

    daemon_threads = True

    def __init__(self, address: tuple[str, int], registry: Registry, runtime_timeout: float = WORLD_TIMEOUT_SECONDS) -> None:
        """
        Bind the HTTP listener and initialize the shared session registry.

        Args:
            address  (tuple[str, int]): Bind host and port; port 0 requests a free port.
            registry (Registry): Validated selectable algorithm catalog.
            runtime_timeout (float): Maximum seconds without completed-tick progress.
        """
        self.runtime_timeout = runtime_timeout
        self.registry = registry
        self.sessions: dict[str, Session] = {}
        self.pending = 0
        self.sessions_lock = threading.Lock()
        self.management_lock = threading.RLock()
        super().__init__(address, Handler)

    def create(self, config: Config) -> tuple[str, Session]:
        """Create an additional world for batch callers, serializing membership changes."""
        with self.management_lock:
            return self._create(config)

    def default_config(self) -> Config:
        """Prefer built-ins, falling back to the first accepted plugin per category."""
        choices = {}
        for category, preferred in ((category, getattr(Config(), category)) for category in ("assignment", "planning")):
            available = [entry["id"] for entry in self.registry.entries if entry["category"] == category]
            if not available:
                raise ValueError(f"No accepted {category} algorithm")
            choices[category] = preferred if preferred in available else available[0]
        return Config(**choices)

    def primary(self, config: Config | None = None) -> tuple[str, Session]:
        """
        Read or initialize the first world, or replace it with explicit settings.

        Args:
            config (Config | None): None reuses the first world without changing
                its state. Explicit settings reset only that world.

        Returns:
            tuple[str, Session]: First world's identifier and session. Additional
                batch worlds retain their order and state after a reset.
        """
        with self.management_lock:
            with self.sessions_lock:
                first = next(iter(self.sessions.items()), None)
            if first is None:
                return self._create(config if config is not None else self.default_config())
            if config is None:
                return first
            old_id, old_session = first
            with old_session.lock:
                session = self._build_session(config)
                try:
                    identity = uuid.uuid4().hex
                    with self.sessions_lock:
                        others = {key: value for key, value in self.sessions.items() if key != old_id}
                        replacement = {identity: session, **others}
                        self.sessions = replacement
                except BaseException:
                    self._discard(session)
                    raise
                self._close_engine(old_session)
                return identity, session

    def delete(self, identity: str) -> bool:
        """Delete a batch-selected world while serializing membership changes."""
        with self.management_lock:
            with self.sessions_lock:
                session = self.sessions.get(identity)
            if session is None:
                return False
            with session.lock:
                with self.sessions_lock:
                    self.sessions.pop(identity)
                self._close_engine(session)
            return True

    @staticmethod
    def _close_engine(session: Session) -> None:
        """Release a supervised worker; direct test engines have no close hook."""
        close = getattr(session.engine, "close", None)
        if callable(close):
            close()

    def _discard(self, session: Session) -> None:
        """Clean a newly built session that failed before publication."""
        self._close_engine(session)
        shutil.rmtree(session.folder, ignore_errors=True)

    def server_close(self) -> None:
        """Close the listener and reap all world workers on service shutdown."""
        super().server_close()
        with self.management_lock:
            for session in self.sessions.values():
                with session.lock:
                    self._close_engine(session)

    def _new_engine(self, config: Config) -> ProcessEngine:
        """Validate choices before starting a supervised world worker."""
        self.registry.parameter_report(config)
        return ProcessEngine(config, self.registry.entries, self.runtime_timeout)

    def primary_snapshot(self) -> dict[str, Any]:
        """Select and serialize the first world atomically with respect to resets."""
        with self.management_lock:
            identity, session = self.primary()
            with session.lock:
                return self.session_snapshot(identity, session)

    @staticmethod
    def session_snapshot(identity: str, session: Session) -> dict[str, Any]:
        """Materialize a response while the caller holds the session lock."""
        return dict(
            world_id=identity, config=session.engine.config.to_dict(),
            parameters=session.engine.parameter_report,
            state=session.engine.snapshot(include_map=True), failed=session.failed,
        )

    def _build_session(self, config: Config) -> Session:
        """Build a world and its output files, cleaning up failed initialization."""
        engine = self._new_engine(config)
        folder = None
        try:
            folder = run_directory("api")
            save_json(folder / "config.json", engine.config.to_dict())
            save_json(folder / "parameters.json", engine.parameter_report)
            return Session(engine, folder)
        except BaseException:
            try:
                self._close_engine(Session(engine, folder or ROOT))
            except Exception:
                # Cleanup must not replace the initialization failure.
                pass
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            raise

    def _create(self, config: Config) -> tuple[str, Session]:
        """
        Initialize and register one independent API simulation world.

        The session limit is reserved before the engine is built, so rejected
        requests do not leave run directories or partially initialized worlds.
        A failed creation releases its reservation and removes any directory that
        was created for it.

        Args:
            config (Config): Validated scenario settings for the new engine.

        Returns:
            tuple[str, Session]: Generated world identifier and its session state.

        Raises:
            ValueError: If the session limit is reached or the algorithm choice is invalid.
        """
        identity = uuid.uuid4().hex
        with self.sessions_lock:
            if len(self.sessions) + self.pending >= MAX_SESSIONS:
                raise ValueError("Session limit reached; delete unused worlds")
            self.pending += 1
        session = None
        try:
            session = self._build_session(config)
            with self.sessions_lock:
                self.sessions[identity] = session
                self.pending -= 1
            return identity, session
        except BaseException:
            with self.sessions_lock:
                self.pending -= 1
            if session is not None:
                self._discard(session)
            raise


class Handler(BaseHTTPRequestHandler):
    """
    Adapt HTTP routes to configuration, catalog and simulation operations.

    Serves only explicitly listed frontend assets. API responses use JSON and
    no-store caching; session locks protect stepping and snapshot serialization.
    """

    @property
    def application(self) -> Server:
        """Return the concrete server installed for this request handler."""
        return cast(Server, self.server)

    def log_message(self, format: str, *args: object) -> None:
        """
        Suppress the default per-request stderr access log.

        Args:
            format (str): Standard access-log format string.
            *args: Standard BaseHTTPRequestHandler log-format arguments.
        """
        pass

    def respond(self, status: int, value: Any) -> None:
        """
        Send a JSON response with explicit UTF-8 encoding and no-store caching.

        Args:
            status (int): HTTP response status.
            value  (Any): JSON-serializable response payload.
        """
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict[str, Any]:
        """
        Read and validate a JSON-object request body.

        Returns:
            dict[str, Any]: Parsed request object.

        Raises:
            ValueError: If media type, body size, JSON object shape or Origin is invalid.
        """
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Expected application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise ValueError("Invalid request size")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        origin = self.headers.get("Origin")
        if origin and origin != f'http://{self.headers.get("Host")}':
            raise ValueError("Origin mismatch")
        return body

    def session(self, identity: str) -> Session:
        """
        Look up a world while protecting the shared session dictionary.

        Args:
            identity (str): API world identifier.

        Returns:
            Session: Session reference; its own lock protects engine operations.

        Raises:
            KeyError: If the world identifier does not exist.
        """
        with self.application.sessions_lock:
            session = self.application.sessions.get(identity)
        if session is None:
            raise KeyError("Unknown world")
        return session

    def do_GET(self) -> None:
        """Convert unexpected read failures to JSON without confusing them with 404."""
        try:
            self._get()
        except Exception as exc:
            self.respond(500, dict(error=f"{type(exc).__name__}: {exc}"))

    def _get(self) -> None:
        """
        Serve health, validated algorithms, world snapshots or frontend assets.

        World snapshots include static map metadata and any stored failure reason.
        Unknown worlds and unlisted routes return HTTP 404.
        """
        route = urlsplit(self.path).path
        if route == "/api/health":
            return self.respond(200, dict(ok=True))
        if route == "/api/settings":
            frontend = dict(FRONTEND_SETTINGS)
            frontend["step_batch_max"] = min(frontend["step_batch_max"], MAX_STEP_TICKS)
            frontend["step_batch_default"] = min(frontend["step_batch_default"], frontend["step_batch_max"])
            return self.respond(200, dict(
                scenario=Config().to_dict(), numeric_bounds=NUMERIC_BOUNDS, frontend=frontend,
            ))
        if route == "/api/algorithms":
            return self.respond(200, self.application.registry.catalog())
        if route == "/api/worlds/first":
            try:
                return self.respond(200, self.application.primary_snapshot())
            except Exception as exc:
                return self.respond(500, dict(error=f"{type(exc).__name__}: {exc}"))
        if route.startswith("/api/worlds/"):
            parts = route.strip("/").split("/")
            if len(parts) != 3:
                return self.respond(404, dict(error="Unknown endpoint"))
            try:
                s = self.session(parts[2])
            except KeyError as exc:
                return self.respond(404, dict(error=str(exc)))
            with s.lock:
                with self.application.sessions_lock:
                    if self.application.sessions.get(parts[2]) is not s:
                        return self.respond(404, dict(error="World was replaced or deleted"))
                return self.respond(200, self.application.session_snapshot(parts[2], s))
        assets = {
            "/": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/styles.css": ("styles.css", "text/css"),
        }
        if route not in assets:
            return self.respond(404, dict(error="Not found"))
        filename, mime = assets[route]
        data = (ROOT / "framework/frontend" / filename).read_bytes()
        if filename == "index.html":
            data = data.replace(b"{{REQUEST_TIMEOUT_MS}}", str(FRONTEND_SETTINGS["request_timeout_ms"]).encode())
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        """
        Reset the first world, create a batch world or advance a bounded tick batch.

        Creation returns HTTP 201 with config and map. Step requests accept 1 to
        the configured MAX_STEP_TICKS and hold the session lock for the full batch. Runtime failures
        are stored and returned as HTTP 422; later step requests return 409. Invalid
        request values return 400, unknown worlds return 404, and unexpected server
        failures return 500 without closing the connection silently.
        """
        route = urlsplit(self.path).path
        try:
            body = self.body()
            if route in ("/api/worlds", "/api/batch/worlds"):
                config = Config.from_dict(body.get("config", {}))
                try:
                    with self.application.management_lock:
                        if route == "/api/batch/worlds":
                            identity, s = self.application.create(config)
                        else:
                            identity, s = self.application.primary(config)
                        with s.lock:
                            response = self.application.session_snapshot(identity, s)
                except ValueError as exc:
                    return self.respond(400, dict(error=str(exc)))
                except Exception as exc:
                    return self.respond(500, dict(error=f"{type(exc).__name__}: {exc}"))
                return self.respond(201, response)
            parts = route.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "worlds"] or parts[3] != "step":
                return self.respond(404, dict(error="Unknown endpoint"))
            try:
                s = self.session(parts[2])
            except KeyError:
                return self.respond(404, dict(error="Unknown world"))
            ticks = body.get("ticks", 1)
            if type(ticks) is not int or not 1 <= ticks <= MAX_STEP_TICKS:
                raise ValueError(f"ticks must be in [1, {MAX_STEP_TICKS}]")
            with s.lock:
                with self.application.sessions_lock:
                    if self.application.sessions.get(parts[2]) is not s:
                        return self.respond(404, dict(error="World was replaced or deleted"))
                if s.failed:
                    return self.respond(409, dict(error="World halted after an algorithm error", detail=s.failed))
                try:
                    state = s.engine.step(ticks)
                except Exception as exc:
                    s.failed = f"{type(exc).__name__}: {exc}"
                    save_json(s.folder / "failure.json", dict(tick=s.engine.world.tick, error=s.failed))
                    return self.respond(422, dict(error=s.failed))
                if state["finished"]:
                    save_json(s.folder / "result.json", state)
                return self.respond(200, dict(state=state))
        except (ValueError, TypeError) as exc:
            self.respond(400, dict(error=str(exc)))
        except Exception as exc:
            self.respond(500, dict(error=f"{type(exc).__name__}: {exc}"))

    def do_DELETE(self) -> None:
        """Convert unexpected deletion failures to a JSON server error."""
        try:
            self._delete()
        except Exception as exc:
            self.respond(500, dict(error=f"{type(exc).__name__}: {exc}"))

    def _delete(self) -> None:
        """
        Remove a world through the batch-management endpoint.

        Returns HTTP 200 when removed and 404 for an unknown world. Persisted
        experiment outputs are retained; this does not delete output directories.
        """
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 4 or parts[:3] != ["api", "batch", "worlds"]:
            return self.respond(404, dict(error="Unknown endpoint"))
        removed = self.application.delete(parts[3])
        self.respond(200 if removed else 404, dict(deleted=removed))

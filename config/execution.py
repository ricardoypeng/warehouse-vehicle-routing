"""Service, CLI and worker supervision defaults; explicit CLI values override defaults."""

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8770
DEFAULT_RUN_TICKS = 3000
DEFAULT_WORKERS = 1

WORLD_TIMEOUT_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 10.0
HEARTBEAT_INTERVAL_SECONDS = 0.5
HEARTBEAT_TIMEOUT_FRACTION = 0.25
PROCESS_EXIT_GRACE_SECONDS = 0.2
PROCESS_TERMINATE_GRACE_SECONDS = 2.0

PROBE_TICKS = 300
PROBE_SCENARIO = {
    "rows": 1,
    "columns": 1,
    "shelf_length": 3,
    "vehicles": 2,
    "tasks": 4,
    "service_ticks": 1,
}

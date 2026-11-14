"""Frontend limits delivered by the service through GET /api/settings."""

SETTINGS = {
    "request_timeout_ms": 120_000,
    "stall_warning_ticks": 10,
    "list_page_size": 50,
    "max_canvas_span": 1600,
    "min_cell_size": 4,
    "max_cell_size": 30,
    "request_rate_min": 1,
    "request_rate_max": 30,
    "request_rate_default": 10,
    "step_batch_min": 1,
    "step_batch_max": 500,
    "step_batch_default": 1,
    "activity_limit": 20,
    "activity_display_limit": 8,
}

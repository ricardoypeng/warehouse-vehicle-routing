"""Reproducible performance scenarios and subprocess deadlines."""

DEFAULT_TICKS = 100
DEFAULT_SEEDS = (0, 1, 2)
TRIAL_TIMEOUT_SECONDS = 600
COMPARISON_TRIAL_TIMEOUT_SECONDS = 1800
SCALES = {
    "default": {},
    "medium": {"rows": 6, "columns": 6, "vehicles": 64, "tasks": 1000},
    "maximum": {"rows": 12, "columns": 12, "shelf_length": 30,
                "shelf_depth": 12, "vehicles": 512, "tasks": 10000},
}

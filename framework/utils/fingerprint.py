"""Fingerprint generated task definitions independently of execution state."""

import hashlib
import json
from typing import Iterable

from ..backend.domain import Task


def task_hash(tasks: Iterable[Task]) -> str:
    """Hash task IDs, endpoint IDs and release ticks in generation order."""
    payload = [(task.id, task.pickup.id, task.dropoff.id, task.release_tick) for task in tasks]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()

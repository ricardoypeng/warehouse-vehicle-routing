"""Intentional hung plugin import, loaded only by the timeout test."""

import time

while True:
    time.sleep(1)

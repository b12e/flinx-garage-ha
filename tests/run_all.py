"""Run every standalone check and report one tally.

    .venv/bin/python -m tests.run_all
"""

from __future__ import annotations

import asyncio
import logging

from . import (
    test_ble,
    test_ble_state,
    test_command_errors,
    test_flows,
    test_migration,
    test_setup,
)
from .harness import summary


async def main() -> None:
    for module in (
        test_migration,
        test_ble,
        test_ble_state,
        test_command_errors,
        test_setup,
        test_flows,
    ):
        await module.main()


if __name__ == "__main__":
    # The integration logs warnings by design on the "BLE unusable" paths.
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())

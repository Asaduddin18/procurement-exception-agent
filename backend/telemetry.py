"""Best-effort Langfuse observations using non-document metadata."""

import logging
import os
from contextlib import contextmanager

LOGGER = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


@contextmanager
def trace_stage(name: str, run_id: str):
    observation = None
    if enabled():
        try:
            from langfuse import get_client

            client = get_client()
            observation = client.start_observation(
                name=name, as_type="span",
                trace_context={"trace_id": client.create_trace_id(seed=str(run_id))},
                metadata={"run_id": str(run_id)},
            )
        except Exception:
            LOGGER.warning("Unable to start policy telemetry.")
    try:
        yield
    finally:
        if observation is not None:
            try:
                observation.end()
            except Exception:
                LOGGER.warning("Unable to close policy telemetry.")


def flush() -> None:
    if enabled():
        try:
            from langfuse import get_client

            get_client().flush()
        except Exception:
            LOGGER.warning("Unable to flush policy telemetry.")

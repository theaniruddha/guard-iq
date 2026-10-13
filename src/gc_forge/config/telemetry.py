from __future__ import annotations

import os

import logfire


def configure_logfire(service_name: str = "guard-iq") -> None:
    token = os.environ.get("LOGFIRE_TOKEN", "")
    if not token:
        return

    logfire.configure(
        token=token,
        service_name=service_name,
        send_to_logfire="if-token-present",
    )
    logfire.instrument_pydantic_ai()
    logfire.instrument_httpx()
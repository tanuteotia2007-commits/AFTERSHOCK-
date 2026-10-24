from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4


class AgentverseConfigurationError(ValueError):
    pass


class AgentverseRequestError(RuntimeError):
    pass


def _send_chat_message(
    address: str,
    text: str,
    timeout: int,
    malformed: bool = False,
) -> str:
    try:
        from uagents.communication import send_message_raw, send_sync_message
        from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
        from uagents_core.envelope import Envelope
        from uagents_core.identity import generate_user_address
        from uagents.types import MsgStatus
    except ImportError as error:
        raise AgentverseRequestError(
            "Agentverse messaging requires uAgents on Python 3.10–3.12; "
            "install backend/requirements.txt with a supported Python runtime"
        ) from error

    message = ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )
    try:
        if malformed:
            response = asyncio.run(
                send_message_raw(
                    destination=address,
                    message_schema_digest=ChatMessage.build_schema_digest(message),
                    message_body="{",
                    sender=generate_user_address(),
                    timeout=timeout,
                    sync=True,
                )
            )
        else:
            response = asyncio.run(
                send_sync_message(destination=address, message=message, timeout=timeout)
            )
    except Exception as error:
        raise AgentverseRequestError(
            f"Agentverse message failed: {type(error).__name__}"
        ) from error

    if isinstance(response, MsgStatus):
        status = getattr(response.status, "value", str(response.status))
        raise AgentverseRequestError(
            f"Agentverse message delivery failed: {status}: {response.detail}"
        )
    if isinstance(response, Envelope):
        return response.decode_payload()
    if isinstance(response, str):
        return response
    if hasattr(response, "model_dump_json"):
        return response.model_dump_json()
    raise AgentverseRequestError("Agentverse returned an unsupported response")


Sender = Callable[[str, str, int, bool], str]


class AgentverseAdapter:
    def __init__(
        self,
        address: str | None = None,
        api_key: str | None = None,
        sender: Sender | None = None,
    ):
        self.address = address if address is not None else os.getenv("AGENTVERSE_AGENT_ADDRESS", "")
        self.api_key = api_key if api_key is not None else os.getenv("AGENTVERSE_API_KEY", "")
        missing = [
            name
            for name, value in (
                ("AGENTVERSE_AGENT_ADDRESS", self.address),
                ("AGENTVERSE_API_KEY", self.api_key),
            )
            if not value.strip()
        ]
        if missing:
            raise AgentverseConfigurationError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        # Mailbox messages use signed envelopes; Agentverse's account API key is not sent to the agent.
        self.sender = sender or _send_chat_message

    def run(
        self,
        message: str,
        fault: str = "none",
        delay_ms: int = 1000,
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise ValueError("message must contain 1 to 2000 characters")
        if fault not in {"none", "delayed_reply", "dropped_message", "duplicated_message", "malformed_payload"}:
            raise ValueError("unsupported Agentverse message fault")
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or not 0 <= delay_ms <= 10_000:
            raise ValueError("delay_ms must be an integer from 0 to 10000")

        events: list[dict[str, Any]] = []

        def log(kind: str, **details: Any) -> None:
            events.append(
                {
                    "seq": len(events),
                    "t_ms": int((time.monotonic() - started) * 1000),
                    "kind": kind,
                    **details,
                }
            )

        started = time.monotonic()

        def redact(value: str) -> str:
            return value.replace(self.api_key, "[REDACTED]") if self.api_key else value

        label = "REAL AGENT"
        log("run_start", execution_type=label, agent_address=self.address, fault=fault)
        if fault != "none":
            label = "REAL AGENT + SIMULATED MESSAGE FAULT"
            log("fault_injected", fault=fault)

        responses: list[str] = []
        error: str | None = None
        if fault == "dropped_message":
            error = "The message was dropped locally; no request was sent to the agent."
            log("message_dropped", delivery="not_sent")
        else:
            malformed = fault == "malformed_payload"
            send_count = 2 if fault == "duplicated_message" else 1
            for index in range(send_count):
                log(
                    "message_sent",
                    attempt=index + 1,
                    malformed_payload=malformed,
                    message_length=len(message),
                )
                try:
                    response = self.sender(
                        self.address,
                        message,
                        timeout_seconds,
                        malformed,
                    )
                except AgentverseRequestError as exception:
                    error = str(exception)
                    log("message_error", attempt=index + 1, error=error)
                    break
                responses.append(response)
                log("reply_received", attempt=index + 1, response=redact(response)[:2000])

            if fault == "delayed_reply" and responses:
                log("reply_delay_started", delay_ms=delay_ms)
                time.sleep(delay_ms / 1000)
                log("reply_delay_completed", delay_ms=delay_ms)

        if fault == "dropped_message":
            violations = [
                {
                    "code": "MESSAGE_DROPPED",
                    "severity": "high",
                    "message": error,
                    "event_seqs": [len(events) - 1],
                }
            ]
        elif error:
            violations = [
                {
                    "code": "AGENTVERSE_REQUEST_FAILED",
                    "severity": "high",
                    "message": error,
                    "event_seqs": [len(events) - 1],
                }
            ]
        else:
            violations = []

        log(
            "agent_result",
            response_count=len(responses),
            error=error,
        )
        scenario_name = f"agentverse-{fault.replace('_', '-')}"
        result = {
            "scenario": {
                "name": scenario_name,
                "message": redact(message),
                "fault": fault,
            },
            "agent": "Agentverse agent",
            "execution_type": label,
            "passed": bool(responses) and error is None,
            "violations": violations,
            "outcome": {"responses": [redact(response) for response in responses]},
            "error": error,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "fingerprint": "",
            "events": events,
        }
        canonical = json.dumps(events, sort_keys=True, default=str).encode("utf-8")
        result["fingerprint"] = hashlib.sha256(canonical).hexdigest()
        return result

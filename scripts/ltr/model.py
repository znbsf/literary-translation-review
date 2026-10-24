"""Model transport shared by every runner (Anthropic Messages wire format).

Authentication comes only from ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``.
Requests, images and history are never logged or persisted; callers keep history
in memory for one session. Redirects are refused so credentials are never
forwarded.
"""
from __future__ import annotations

import copy
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any, Callable

from ltr.core import ContractError, need

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_FIELDS = 32
MAX_HISTORY_BLOCKS = 1000
HISTORY_MODES = frozenset({"text", "preserve-thinking"})
_REASONING_FIELDS = frozenset({"thinking", "redacted_thinking", "reasoning", "reasoning_content",
    "reasoning_details", "thinking_signature", "reasoning_signature", "signature", "analysis", "thoughts",
    "tool_calls"})
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

Transport = Callable[[bytes, str], dict]


class ModelError(ContractError):
    pass


def encode_request(model: str, message: dict, history: list[dict], session_id: str, max_output_tokens: int,
                   *, system: str) -> bytes:
    return json.dumps({"model": model, "max_tokens": max_output_tokens, "system": system,
                       "messages": [*history, message],
                       "metadata": {"user_id": json.dumps({"device_id": "corpus-runtime", "account_uuid": "",
                                                           "session_id": session_id})}},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def image_message(image: bytes, media_type: str, text: str) -> dict:
    import base64
    return {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.b64encode(image).decode("ascii")}},
        {"type": "text", "text": text}]}


def assistant_reply(response: Any, history_mode: str) -> tuple[str, dict, dict]:
    """Validate a completed response; return (text, assistant history message, block counts).

    ``preserve-thinking`` keeps thinking blocks and signatures in history, in
    order, so a long session stays valid; only text is parsed as the answer.
    """
    need(history_mode in HISTORY_MODES, "invalid_history_mode", ModelError)
    need(type(response) is dict and len(response) <= MAX_RESPONSE_FIELDS, "transport_contract_invalid", ModelError)
    need(not _REASONING_FIELDS.intersection(response), "unsupported_response_content", ModelError)
    need(response.get("stop_reason") == "end_turn", "incomplete_model_response", ModelError)
    content = response.get("content")
    need(type(content) is list and 0 < len(content) <= MAX_HISTORY_BLOCKS, "transport_contract_invalid", ModelError)
    counts = {"blocks": len(content), "text": 0, "thinking": 0, "redacted_thinking": 0}
    texts, blocks, size = [], [], 0
    for original in list(content):
        need(type(original) is dict, "unsupported_response_content", ModelError)
        block = original.copy()
        kind = block.get("type")
        if kind == "text":
            need(set(block) == {"type", "text"} and type(block["text"]) is str, "unsupported_response_content", ModelError)
            texts.append(block["text"])
        elif kind == "thinking" and history_mode == "preserve-thinking":
            need(set(block) == {"type", "thinking", "signature"}
                 and all(type(block[k]) is str for k in ("thinking", "signature")), "unsupported_response_content", ModelError)
        elif kind == "redacted_thinking" and history_mode == "preserve-thinking":
            need(set(block) == {"type", "data"} and type(block["data"]) is str, "unsupported_response_content", ModelError)
        else:
            raise ModelError("unsupported_response_content")
        size += sum(len(v) for v in block.values())
        need(size <= MAX_RESPONSE_BYTES, "response_limits_exceeded", ModelError)
        blocks.append(block)
        counts[kind] += 1
    text = "".join(texts)
    assistant = {"role": "assistant",
                 "content": copy.deepcopy(blocks) if history_mode == "preserve-thinking" else text}
    return text, assistant, counts


def usage(response: Any) -> dict[str, int]:
    value = response.get("usage") if type(response) is dict else None
    if type(value) is not dict:
        return {}
    return {k: value[k] for k in USAGE_KEYS if type(value.get(k)) is int and 0 <= value[k] < 1 << 53}


def image_info(raw: bytes) -> tuple[tuple[int, int], str]:
    """(width, height), media type — verified with Pillow, decompression bombs refused."""
    try:
        from PIL import Image
    except ImportError:
        raise ModelError("image_dependency_unavailable") from None
    kinds = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                size, kind = image.size, image.format
                image.verify()
    except Exception:
        raise ModelError("image_invalid") from None
    need(kind in kinds, "image_format_unsupported", ModelError)
    return size, kinds[kind]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        response.close()
        raise urllib.error.HTTPError(request.full_url, code, "redirect_rejected", headers, None)


def environment_transport(timeout_seconds: int = 300) -> Transport:
    """Transport authenticated from the environment; fails early if it is not configured."""
    endpoint = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    parsed = urllib.parse.urlsplit(endpoint)
    need(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
         and not parsed.query and not parsed.fragment and token
         and not any(ord(c) < 32 for c in endpoint + token), "runtime_authentication_unavailable", ModelError)
    need(type(timeout_seconds) is int and 1 <= timeout_seconds <= 900, "invalid_request_timeout", ModelError)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())

    def send(encoded: bytes, session_id: str) -> dict:
        request = urllib.request.Request(endpoint.rstrip("/") + "/v1/messages", data=encoded, headers={
            "Content-Type": "application/json", "x-api-key": token, "anthropic-version": "2023-06-01",
            "X-Session-Id": session_id})
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        need(len(raw) <= MAX_RESPONSE_BYTES, "response_limits_exceeded", ModelError)
        return json.loads(raw.decode("utf-8"))

    return send

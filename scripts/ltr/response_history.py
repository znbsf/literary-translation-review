"""Pure in-memory assistant block contracts. No transport or persistence."""
import copy
import json

HISTORY_MODES = ("text", "preserve-thinking")
TOP_LEVEL_FIELDS = frozenset({"thinking", "redacted_thinking", "reasoning", "reasoning_content", "reasoning_details",
    "thinking_signature", "reasoning_signature", "signature", "analysis", "thoughts", "tool_calls"})
MAX_BLOCKS = 1000
MAX_CONTENT_BYTES = 2 * 1024 * 1024


def resolve_history_mode(value="text"):
    if type(value) is not str or value not in HISTORY_MODES:
        raise ValueError("invalid_history_mode")
    return value


def assistant_content(content, history_mode="text"):
    mode = resolve_history_mode(history_mode)
    if type(content) is not list or not content or (mode != "text" and len(content) > MAX_BLOCKS):
        raise ValueError("unsupported_response_content")
    snapshots, parts, size = [], [], 0
    counts = dict.fromkeys(("blocks", "text", "thinking", "redacted_thinking"), 0)
    for original in list(content):
        if type(original) is not dict:
            raise ValueError("unsupported_response_content")
        block = original.copy()
        kind = block.get("type")
        if kind == "text":
            fields = {"type", "text"}
        elif kind == "thinking" and mode == "preserve-thinking":
            fields = {"type", "thinking", "signature"}
        elif kind == "redacted_thinking" and mode == "preserve-thinking":
            fields = {"type", "data"}
        else:
            raise ValueError("unsupported_response_content")
        if set(block) != fields or not all(type(value) is str for value in block.values()):
            raise ValueError("unsupported_response_content")
        try:
            size += sum(len(value.encode("utf-8")) for value in block.values())
        except UnicodeEncodeError:
            raise ValueError("unsupported_response_content") from None
        if mode != "text" and size > MAX_CONTENT_BYTES:
            raise ValueError("unsupported_response_content")
        snapshots.append(block)
        counts[kind] += 1
        counts["blocks"] += 1
        if kind == "text":
            parts.append(block["text"])
    text = "".join(parts).strip()  # Historical default domain/text-history behavior.
    if mode == "preserve-thinking":
        # Bound the complete serialized assistant message, including JSON field
        # names, punctuation and escaping, using the request encoder's format.
        serialized = json.dumps({"role": "assistant", "content": snapshots}, ensure_ascii=False).encode("utf-8")
        if len(serialized) > MAX_CONTENT_BYTES:
            raise ValueError("unsupported_response_content")
    return text, copy.deepcopy(snapshots) if mode == "preserve-thinking" else text, counts


def validate_history(history, history_mode="text"):
    mode = resolve_history_mode(history_mode)
    if type(history) is not list:
        raise ValueError("invalid_session")
    try:
        for message in history:
            if type(message) is not dict or set(message) != {"role", "content"} or message["role"] not in ("user", "assistant"):
                raise ValueError("invalid_session")
            if message["role"] == "assistant" and mode == "preserve-thinking":
                assistant_content(message["content"], mode)
            elif type(message["content"]) is not str:
                raise ValueError("invalid_session")
            else:
                message["content"].encode("utf-8")
    except (ValueError, UnicodeEncodeError):
        raise ValueError("invalid_session") from None


def safe_counts(value):
    keys = ("blocks", "text", "thinking", "redacted_thinking")
    if type(value) is not dict or not all(type(value.get(key)) is int and 0 <= value[key] <= MAX_BLOCKS for key in keys):
        return {}
    return {key: value[key] for key in keys}

"""LiteLLM proxy hook: normalize codex Responses input for chat-only vendors.

codex talks the Responses API; DeepSeek / Z.ai / Moonshot only serve
/chat/completions, so LiteLLM's Responses->Chat bridge maps each Responses input
item to a chat message: `function_call` -> an assistant message with ONE
`tool_calls` entry, `function_call_output` -> a `tool` message. Chat vendors
enforce: every assistant `tool_calls` message must be immediately followed by
the `tool` result(s) for its call id(s). codex's input violates this in two ways:

  1. It wedges non-tool items between a call and its output -- an empty
     `assistant` placeholder message (content "") emitted alongside the call.
  2. It batches parallel tool calls as consecutive `function_call` items
     (call, call, output, output), which the one-call-per-message bridge turns
     into two adjacent assistant messages -> the first has no tool result after
     it.

We normalize the input so the chat projection is always valid:
  - drop empty assistant placeholder messages, and
  - reorder so each `function_call` is immediately followed by its matching
    `function_call_output` (paired by call_id), leaving preceding `reasoning`
    items attached to their call.

`reasoning` items are DELIBERATELY KEPT: DeepSeek's thinking mode rejects a
continued tool-calling conversation whose assistant turn omits the
`reasoning_content` it produced ("reasoning_content in the thinking mode must be
passed back"). The bridge maps a reasoning item that precedes a function_call
onto that assistant message, satisfying the vendor.
"""

from litellm.integrations.custom_logger import CustomLogger


def _item_text(item):
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "")
                       for p in content if isinstance(p, dict))
    return ""


def _is_noise(item):
    """Empty assistant placeholder message -> drop from history."""
    if not isinstance(item, dict):
        return False
    if (item.get("type") == "message"
            and item.get("role") == "assistant"
            and not _item_text(item).strip()):
        return True
    return False


def sanitize_input(data):
    inp = data.get("input")
    if not isinstance(inp, list):
        return data

    # Index tool outputs by call_id so each call can be paired adjacently.
    outputs = {}
    for it in inp:
        if isinstance(it, dict) and it.get("type") == "function_call_output":
            cid = it.get("call_id")
            if cid is not None:
                outputs.setdefault(cid, []).append(it)

    emitted_outputs = set()
    result = []
    for it in inp:
        if _is_noise(it):
            continue
        if isinstance(it, dict) and it.get("type") == "function_call_output":
            # Emitted right after its call below; skip here to avoid duplication
            # (only skip once per call_id in case of odd repeats).
            cid = it.get("call_id")
            if cid in emitted_outputs:
                continue
            # Orphan output with no preceding call: keep it in place.
            result.append(it)
            continue
        result.append(it)
        if isinstance(it, dict) and it.get("type") == "function_call":
            cid = it.get("call_id")
            for out in outputs.get(cid, []):
                result.append(out)
            if cid is not None:
                emitted_outputs.add(cid)

    if result != inp:
        data["input"] = result
    return data


class ReasoningStripper(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # call_type is "responses"/"aresponses" for the /v1/responses endpoint.
        if "input" in data:
            sanitize_input(data)
        return data


reasoning_stripper = ReasoningStripper()

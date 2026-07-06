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

THINKING: codex sends its configured `model_reasoning_effort` to the bridge as
Responses-style reasoning metadata. We normalize the four open model routes to
thinking-on here so parity does not depend on LiteLLM/provider defaults:
  - GLM-5.2: Z.ai thinking enabled + provider effort `high` (medium-equivalent;
    Z.ai exposes high/max, not medium).
  - GLM-4.7 Flash, DeepSeek V4 Flash, Kimi K2.7 Code: thinking enabled with the
    vendor's default thinking effort (no portable medium level on this route).

TOOLS: codex advertises non-`function` Responses tool types -- `namespace`
(grouped MCP / multi-agent tool bundles), `web_search`, `image_generation`.
Some chat vendors strictly 400 on these (Z.ai: "tools[N].type:type is illegal";
Moonshot: "unknown tool type: namespace"). We sanitize the tools array uniformly
for all bridge chat-vendor routes: keep `function` tools as-is, coerce a
non-function tool that still carries a name + description + parameters into a
function tool, and DROP the rest (logging which). The dropped types are
capabilities codex cannot exercise in the bench sandbox anyway; the real work
tools (exec_command, write_stdin, update_plan, ...) are all `function`.

DEEPSEEK REASONING CONTENT: LiteLLM maps incoming Responses `reasoning` items
into assistant content before merging the following `function_call` into the same
assistant message. DeepSeek thinking mode requires that text to be passed back as
`reasoning_content`, not plain assistant `content`; the pre-request hook moves it
onto the tool-call assistant message before the upstream chat request is sent.
"""

from litellm.integrations.custom_logger import CustomLogger
from litellm._logging import verbose_proxy_logger


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


def _model_key(model):
    return model.lower() if isinstance(model, str) else ""


def _is_chat_vendor_route(model):
    """True for the chat-only open-model routes served by this bridge."""
    model = _model_key(model)
    return model in {
        "deepseek-v4-flash",
        "glm-5.2",
        "glm-4.7-flash",
        "kimi-k2.7-code",
    }


def _is_zai_route(model):
    """Backward-compatible test helper for the Z.ai/GLM model group."""
    return _model_key(model).startswith("glm")


def _merge_extra_body(data, updates):
    extra = data.get("extra_body")
    if not isinstance(extra, dict):
        extra = {}
    extra.update(updates)
    data["extra_body"] = extra


def normalize_thinking(data):
    """Force thinking-on parity for codex open-model bridge requests.

    LiteLLM's Responses->Chat bridge may drop provider-specific params before
    they reach chat-only vendors. Set both the normalized request fields and
    `extra_body` copies so the provider-native controls survive conversion and
    OpenAI-compatible SDK filtering.
    """
    model = _model_key(data.get("model"))
    if model == "glm-5.2":
        data["thinking"] = {"type": "enabled", "clear_thinking": False}
        data["reasoning"] = {"effort": "high"}
        data["reasoning_effort"] = "high"
        # Z.ai exposes high/max; high is the closest medium-equivalent.
        _merge_extra_body(data, {"thinking": data["thinking"], "reasoning_effort": "high"})
    elif model == "glm-4.7-flash":
        data["thinking"] = {"type": "enabled", "clear_thinking": False}
        _merge_extra_body(data, {"thinking": data["thinking"]})
        data.pop("reasoning", None)
        data.pop("reasoning_effort", None)
    elif model == "deepseek-v4-flash":
        data["thinking"] = {"type": "enabled"}
        # DeepSeek's LiteLLM chat adapter explicitly supports top-level
        # `thinking`; do not duplicate it in extra_body, which bypasses the
        # adapter's reasoning_content preservation path.
        # DeepSeek V4 Flash has high/max but no medium; do not send codex's
        # medium string upstream as a level. The thinking object requests the
        # vendor's default thinking-on behavior.
        data.pop("reasoning", None)
        data.pop("reasoning_effort", None)
    elif model == "kimi-k2.7-code":
        # Moonshot Kimi reasoning models use DeepSeek-style thinking controls on
        # the OpenAI-compatible endpoint. No portable medium effort is exposed.
        data["thinking"] = {"type": "enabled"}
        _merge_extra_body(data, {"thinking": data["thinking"]})
        data.pop("reasoning", None)
        data.pop("reasoning_effort", None)
    return data


def normalize_chat_thinking(model, kwargs):
    """Apply thinking controls again after Responses->Chat conversion.

    Proxy `async_pre_call_hook` sees the incoming Responses request; these hooks
    see the final chat-completions kwargs. Re-applying here prevents LiteLLM's
    Responses adapter or provider supported-param filtering from dropping the
    vendor-native thinking controls.
    """
    if not isinstance(kwargs, dict):
        return kwargs
    model = _model_key(model or kwargs.get("model"))
    if "glm-5.2" in model:
        thinking = {"type": "enabled", "clear_thinking": False}
        kwargs["thinking"] = thinking
        kwargs["reasoning_effort"] = "high"
        _merge_extra_body(kwargs, {"thinking": thinking, "reasoning_effort": "high"})
    elif "glm-4.7-flash" in model:
        thinking = {"type": "enabled", "clear_thinking": False}
        kwargs["thinking"] = thinking
        kwargs.pop("reasoning_effort", None)
        _merge_extra_body(kwargs, {"thinking": thinking})
    elif "deepseek-v4-flash" in model:
        thinking = {"type": "enabled"}
        kwargs["thinking"] = thinking
        kwargs.pop("reasoning_effort", None)
    elif "kimi-k2.7-code" in model:
        thinking = {"type": "enabled"}
        kwargs["thinking"] = thinking
        kwargs.pop("reasoning_effort", None)
        _merge_extra_body(kwargs, {"thinking": thinking})
    return kwargs


def _coerce_tool(tool):
    """Return a Z.ai-legal `function` tool, or None to drop it.

    A `function` tool passes through. Any other type is coerced only if it still
    carries the shape of a function (name + description + object parameters);
    otherwise it is dropped.
    """
    if not isinstance(tool, dict):
        return None
    if tool.get("type") == "function":
        return tool
    name = tool.get("name")
    params = tool.get("parameters")
    if name and tool.get("description") is not None and isinstance(params, dict):
        coerced = {"type": "function", "name": name,
                   "description": tool["description"], "parameters": params}
        if "strict" in tool:
            coerced["strict"] = tool["strict"]
        return coerced
    return None


def sanitize_tools(data):
    tools = data.get("tools")
    if not isinstance(tools, list):
        return data
    kept, dropped = [], []
    for t in tools:
        coerced = _coerce_tool(t)
        if coerced is not None:
            kept.append(coerced)
        elif isinstance(t, dict):
            dropped.append(f"{t.get('type')}:{t.get('name')}")
    if kept != tools:
        data["tools"] = kept
    if dropped:
        verbose_proxy_logger.info(
            "bridge: dropped %d non-function tool(s) for chat-vendor route: %s",
            len(dropped), ", ".join(dropped))
    return data


def _get_field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _set_field(obj, name, value):
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


def _message_text(message):
    content = _get_field(message, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(getattr(part, "text", "") or ""))
        return "".join(parts)
    return ""


def _has_tool_calls(message):
    tool_calls = _get_field(message, "tool_calls")
    return isinstance(tool_calls, list) and len(tool_calls) > 0


def preserve_deepseek_reasoning_content(model, messages):
    """Move Responses reasoning text onto DeepSeek tool-call messages.

    DeepSeek rejects the next thinking-mode turn unless the assistant tool-call
    message includes the exact prior `reasoning_content`. LiteLLM's
    Responses->Chat input transformer may leave that text as ordinary assistant
    content, so convert it at the last possible hook before provider dispatch.
    """
    if "deepseek-v4-flash" not in _model_key(model) or not isinstance(messages, list):
        return messages

    drop_indexes = set()
    moved = 0
    for idx, msg in enumerate(messages):
        if _get_field(msg, "role") != "assistant" or not _has_tool_calls(msg):
            continue
        if _get_field(msg, "reasoning_content"):
            continue
        reasoning_text = _message_text(msg).strip()
        if not reasoning_text and idx > 0:
            prev = messages[idx - 1]
            if _get_field(prev, "role") == "assistant" and not _has_tool_calls(prev):
                reasoning_text = _message_text(prev).strip()
                if reasoning_text:
                    drop_indexes.add(idx - 1)
        if reasoning_text:
            _set_field(msg, "reasoning_content", reasoning_text)
            _set_field(msg, "content", None)
            moved += 1

    if drop_indexes:
        messages[:] = [m for i, m in enumerate(messages) if i not in drop_indexes]
    if moved:
        verbose_proxy_logger.info(
            "bridge: moved reasoning_content onto %d DeepSeek tool-call message(s)",
            moved)
    return messages


def _deepseek_thinking_mode_active_for_bridge(model, optional_params):
    """Bridge capability gate for custom DeepSeek model groups.

    LiteLLM's DeepSeek adapter already fills missing `reasoning_content`, but in
    this bridge the routed model group is `deepseek-v4-flash`, which can miss
    LiteLLM's built-in supports_reasoning lookup even when explicit thinking is
    active. Treat an explicit DeepSeek thinking object as authoritative.
    """
    return (
        "deepseek-v4-flash" in _model_key(model)
        and isinstance(optional_params, dict)
        and (optional_params.get("thinking") or {}).get("type") == "enabled"
    )


def _patch_deepseek_thinking_gate():
    try:
        from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig
    except Exception:  # noqa: BLE001 - tests stub litellm; bridge can still run.
        return
    if getattr(DeepSeekChatConfig, "_openbench_thinking_gate_patched", False):
        return
    original = DeepSeekChatConfig._thinking_mode_active

    def patched(self, model, optional_params):
        if _deepseek_thinking_mode_active_for_bridge(model, optional_params):
            return True
        return original(self, model, optional_params)

    DeepSeekChatConfig._thinking_mode_active = patched
    DeepSeekChatConfig._openbench_thinking_gate_patched = True


_patch_deepseek_thinking_gate()


class ReasoningStripper(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # call_type is "responses"/"aresponses" for the /v1/responses endpoint.
        normalize_thinking(data)
        if "input" in data:
            sanitize_input(data)
        if _is_chat_vendor_route(data.get("model")):
            sanitize_tools(data)
        return data

    async def async_pre_request_hook(self, model, messages, kwargs):
        normalize_chat_thinking(model, kwargs)
        preserve_deepseek_reasoning_content(model, messages)
        return kwargs

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        normalize_chat_thinking(kwargs.get("model"), kwargs)
        preserve_deepseek_reasoning_content(kwargs.get("model"), kwargs.get("messages"))
        return kwargs


reasoning_stripper = ReasoningStripper()

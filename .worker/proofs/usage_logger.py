import json, os, time
from litellm.integrations.custom_logger import CustomLogger


def _plain(obj):
    """Convert usage-like objects to JSON primitives.

    This helper is intentionally used only on usage objects, not full model
    responses, so prompt/output text and tool-call arguments are never logged.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj[:20]]
    for meth in ('model_dump', 'dict'):
        fn = getattr(obj, meth, None)
        if callable(fn):
            try:
                return _plain(fn())
            except Exception:
                pass
    return repr(obj)


def _usage_from(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return _plain(obj.get('usage') or obj.get('response', {}).get('usage'))
    usage = getattr(obj, 'usage', None)
    return _plain(usage) if usage is not None else None


def _finish_reason_from(obj):
    """Best-effort non-sensitive response metadata for grouping records."""
    try:
        choices = obj.get('choices') if isinstance(obj, dict) else getattr(obj, 'choices', None)
        if not choices:
            return None
        first = choices[0]
        return first.get('finish_reason') if isinstance(first, dict) else getattr(first, 'finish_reason', None)
    except Exception:
        return None


def _has_tool_calls(obj):
    """Return only a boolean; never serialize tool-call names/arguments."""
    try:
        choices = obj.get('choices') if isinstance(obj, dict) else getattr(obj, 'choices', None)
        if not choices:
            return None
        first = choices[0]
        message = first.get('message') if isinstance(first, dict) else getattr(first, 'message', None)
        if message is None:
            return None
        tool_calls = message.get('tool_calls') if isinstance(message, dict) else getattr(message, 'tool_calls', None)
        return bool(tool_calls)
    except Exception:
        return None


class UsageLogger(CustomLogger):
    def _write(self, event, **payload):
        path = os.environ.get('OPENBENCH_USAGE_LOG')
        if not path:
            return
        rec = {'ts': time.time(), 'event': event, **payload}
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + '\n')

    def _write_success(self, event, source, response_obj):
        self._write(
            event,
            model=source.get('model') if isinstance(source, dict) else None,
            call_type=source.get('call_type') if isinstance(source, dict) else None,
            finish_reason=_finish_reason_from(response_obj),
            has_tool_calls=_has_tool_calls(response_obj),
            usage=_usage_from(response_obj),
        )

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._write_success('async_log_success_event', kwargs, response_obj)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._write_success('log_success_event', kwargs, response_obj)

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        self._write_success('async_post_call_success_hook', data, response)
        return response

usage_logger = UsageLogger()

"""Parameterise and expand pipeline configuration dictionaries.

Pipelines are often written once as a *template* and instantiated many times
with different parameters: the same extract/transform/load shape for a dozen
data sources, or a fan-out of near-identical tasks over a list of shards. Hand
writing every variant is tedious and error prone. This module lets a config dict
carry ``${name}`` placeholders and per-task expansion directives, and renders
them into a concrete config that :func:`taskflow.config.load_pipeline` accepts.

Two mechanisms, applied in this order:

1. **Placeholder substitution.** Any string value of the form ``"${key}"`` (or a
   string embedding ``${key}`` among other text) is replaced from a supplied
   ``params`` mapping. A bare ``"${key}"`` that fills the whole string takes the
   parameter's *native* type (so ``"${count}"`` with ``count=3`` becomes the
   integer ``3``); an embedded placeholder is string-interpolated.

2. **Task expansion.** A task carrying a ``for_each`` directive is expanded into
   one task per item in the referenced list, with the loop variable available to
   the placeholders in that task. Ids are made unique per item.

Neither step ever executes anything or imports the scheduler; it is pure dict-to
dict transformation, so it composes cleanly with :mod:`taskflow.validation`
(validate the *rendered* config) and :mod:`taskflow.config` (load it).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Matches a ``${identifier}`` placeholder. Identifiers are the usual
# letters/digits/underscore, plus dots for nested access into the loop item.
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")


class TemplateError(Exception):
    """Raised when a template references an unknown parameter or is malformed."""


def _lookup(name: str, params: Dict[str, Any]) -> Any:
    """Resolve a possibly-dotted ``name`` against ``params``.

    ``"shard"`` looks up ``params["shard"]``; ``"item.host"`` looks up
    ``params["item"]["host"]``. A missing key raises :class:`TemplateError` so a
    typo surfaces at render time rather than becoming a silent empty string.
    """

    parts = name.split(".")
    value: Any = params
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise TemplateError("unknown template parameter: {!r}".format(name))
    return value


def substitute_string(text: str, params: Dict[str, Any]) -> Any:
    """Substitute ``${...}`` placeholders in ``text``.

    A string that is *exactly* one placeholder returns the parameter's native
    value (preserving int/list/dict types). A string with a placeholder embedded
    in other text returns a string with each placeholder stringified in place.
    """

    match = _PLACEHOLDER.fullmatch(text)
    if match:
        return _lookup(match.group(1), params)

    def repl(m: "re.Match[str]") -> str:
        return str(_lookup(m.group(1), params))

    return _PLACEHOLDER.sub(repl, text)


def substitute(value: Any, params: Dict[str, Any]) -> Any:
    """Recursively substitute placeholders throughout a nested structure.

    Dicts and lists are walked; strings are run through
    :func:`substitute_string`; every other scalar is returned unchanged. The
    input is never mutated -- a fresh structure is built.
    """

    if isinstance(value, str):
        return substitute_string(value, params)
    if isinstance(value, dict):
        return {key: substitute(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, params) for item in value]
    return value


def _expand_task(
    task: Dict[str, Any], params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Expand one task dict, honouring a ``for_each`` directive if present.

    Without ``for_each`` the task is returned as a single substituted dict. With
    it, the directive names a loop variable and the list to iterate; the list may
    itself be a placeholder resolving to a ``params`` list. Each produced task
    gets the loop item bound under the loop-variable name and, unless it sets its
    own id template, a per-item id suffix so ids stay unique.
    """

    directive = task.get("for_each")
    if directive is None:
        return [substitute(task, params)]

    if not isinstance(directive, dict) or "var" not in directive or "in" not in directive:
        raise TemplateError(
            "for_each must be a mapping with 'var' and 'in' keys"
        )
    var = directive["var"]
    source = directive["in"]
    if isinstance(source, str):
        source = substitute_string(source, params)
    if not isinstance(source, list):
        raise TemplateError("for_each 'in' must resolve to a list")

    body = {k: v for k, v in task.items() if k != "for_each"}
    has_id_template = isinstance(body.get("id"), str) and "${" in body["id"]

    expanded: List[Dict[str, Any]] = []
    for index, item in enumerate(source):
        scoped = dict(params)
        scoped[var] = item
        scoped[var + "_index"] = index
        rendered = substitute(body, scoped)
        if not has_id_template and "id" in rendered:
            rendered["id"] = "{}-{}".format(rendered["id"], index)
        expanded.append(rendered)
    return expanded


def render(config: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Render a template ``config`` into a concrete pipeline config dict.

    Applies placeholder substitution everywhere and expands any ``for_each``
    tasks, returning a new config dict ready for :func:`taskflow.validation.validate_config`
    and :func:`taskflow.config.load_pipeline`. The ``params`` mapping supplies the
    values placeholders resolve against; an empty or omitted mapping renders a
    config that happens to contain no placeholders unchanged.

    Raises :class:`TemplateError` if a placeholder references an unknown
    parameter or a ``for_each`` directive is malformed.
    """

    params = dict(params) if params else {}
    if not isinstance(config, dict):
        raise TemplateError("template config must be a mapping")

    rendered: Dict[str, Any] = {}
    for key, value in config.items():
        if key == "tasks":
            continue
        rendered[key] = substitute(value, params)

    tasks = config.get("tasks", [])
    if not isinstance(tasks, list):
        raise TemplateError("'tasks' must be a list")

    rendered_tasks: List[Dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise TemplateError("each task must be a mapping")
        rendered_tasks.extend(_expand_task(task, params))
    rendered["tasks"] = rendered_tasks
    return rendered


def placeholders(config: Any) -> List[str]:
    """Return every distinct placeholder name referenced in ``config``.

    Walks the structure collecting the identifiers inside ``${...}`` markers, in
    first-seen order. Useful for a tool that wants to prompt for, or validate the
    presence of, the parameters a template needs before rendering it.
    """

    found: List[str] = []
    seen = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            for match in _PLACEHOLDER.finditer(value):
                name = match.group(1)
                if name not in seen:
                    seen.add(name)
                    found.append(name)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(config)
    return found


def required_params(config: Any) -> List[str]:
    """Return the top-level parameter names a template needs to render.

    Like :func:`placeholders` but collapses dotted references to their root
    (``"item.host"`` -> ``"item"``) and drops the loop variables introduced by
    ``for_each`` directives, leaving just the externally-supplied parameters a
    caller must provide.
    """

    loop_vars = set()

    def collect_loop_vars(value: Any) -> None:
        if isinstance(value, dict):
            directive = value.get("for_each")
            if isinstance(directive, dict) and "var" in directive:
                loop_vars.add(directive["var"])
            for item in value.values():
                collect_loop_vars(item)
        elif isinstance(value, list):
            for item in value:
                collect_loop_vars(item)

    collect_loop_vars(config)

    roots: List[str] = []
    seen = set()
    for name in placeholders(config):
        root = name.split(".")[0]
        if root in loop_vars or root.endswith("_index"):
            continue
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots

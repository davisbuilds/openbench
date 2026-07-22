"""LangSmith evaluators for OpenBench result examples.

The deterministic evaluator is uploadable as a custom code evaluator. The
LLM-as-judge evaluators are intended for local ``evaluate()`` runs because the
LangSmith CLI does not currently upload structured LLM judges.
"""

from __future__ import annotations

import json
from typing import Any


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def _outputs(obj: Any) -> dict[str, Any]:
    outputs = _value(obj, "outputs", {})
    return outputs if isinstance(outputs, dict) else {}


def _inputs(obj: Any) -> dict[str, Any]:
    inputs = _value(obj, "inputs", {})
    return inputs if isinstance(inputs, dict) else {}


def checker_verdict_agreement(run: Any, example: Any) -> dict[str, Any]:
    """Check that success agrees with the recorded checker exit status."""
    actual = _outputs(run)
    success = actual.get("success")
    checker_exit = actual.get("checker_exit")
    expected = checker_exit == 0 if checker_exit is not None else None
    score = int(expected is not None and success == expected)
    return {
        "score": score,
        "comment": f"success={success!r}, checker_exit={checker_exit!r}, "
        f"expected_success={expected!r}",
    }


async def _judge(run: Any, example: Any, dimension: str) -> dict[str, Any]:
    """Ask a structured-output LLM judge to score one qualitative dimension."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "LLM judges require langchain-openai; "
            "install it before running them"
        ) from exc

    from typing import Annotated, TypedDict

    class Grade(TypedDict):
        reasoning: Annotated[str, "Explain the judgment"]
        score: Annotated[int, "Integer score from 0 through 1"]

    judge = ChatOpenAI(
        model="gpt-4o-mini", temperature=0
    ).with_structured_output(Grade, method="json_schema", strict=True)
    inputs = _inputs(example)
    outputs = _outputs(run)
    prompt = (
        f"Evaluate the OpenBench coding-agent result on {dimension}.\n\n"
        f"Task instruction:\n{inputs.get('instruction', '')}\n\n"
        "Checker evidence:\n"
        f"{json.dumps(outputs, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Return score 1 only when the result is good on {dimension}; "
        "otherwise 0. "
        "Explain the judgment."
    )
    grade = await judge.ainvoke([{"role": "user", "content": prompt}])
    return {"score": int(bool(grade["score"])), "comment": grade["reasoning"]}


async def correctness_judge(run: Any, example: Any) -> dict[str, Any]:
    return await _judge(
        run, example, "correctness relative to the specification"
    )


async def root_cause_judge(run: Any, example: Any) -> dict[str, Any]:
    return await _judge(
        run, example, "whether the agent addressed the root cause"
    )


async def response_quality_judge(run: Any, example: Any) -> dict[str, Any]:
    return await _judge(
        run, example, "final response quality and completeness"
    )

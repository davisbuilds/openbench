import json
import tempfile
import unittest
from pathlib import Path

from bench.atif import validate_trajectory
from bench.tools.atif_convert import convert_file, main as atif_main


class ATIFTests(unittest.TestCase):
    def _write(self, name, body):
        path = Path(self.tmp.name) / name
        path.write_text(body)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_validator_rejects_bad_step_order(self):
        errors = validate_trajectory({
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "x", "version": "1"},
            "steps": [{"step_id": 2, "source": "agent", "message": "hi"}],
        })
        self.assertTrue(any("step_id" in e for e in errors))

    def test_validator_reports_malformed_total_cost(self):
        errors = validate_trajectory({
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "x", "version": "1"},
            "steps": [
                {
                    "step_id": 1,
                    "source": "agent",
                    "message": "hi",
                    "metrics": {"cost_usd": 0.1},
                }
            ],
            "final_metrics": {"total_steps": 1, "total_cost_usd": "unknown"},
        })
        self.assertTrue(any("total_cost_usd" in e for e in errors))

    def test_validator_rejects_non_finite_and_bool_costs(self):
        for bad_cost in (float("nan"), float("inf"), True):
            with self.subTest(bad_cost=bad_cost):
                errors = validate_trajectory({
                    "schema_version": "ATIF-v1.7",
                    "agent": {"name": "x", "version": "1"},
                    "steps": [
                        {
                            "step_id": 1,
                            "source": "agent",
                            "message": "hi",
                            "metrics": {"cost_usd": bad_cost},
                        }
                    ],
                    "final_metrics": {"total_steps": 1},
                })
                self.assertTrue(any("cost_usd" in e for e in errors), errors)

    def test_validator_rejects_non_finite_final_cost(self):
        errors = validate_trajectory({
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "x", "version": "1"},
            "steps": [{"step_id": 1, "source": "agent", "message": "hi"}],
            "final_metrics": {"total_steps": 1, "total_cost_usd": float("inf")},
        })
        self.assertTrue(any("total_cost_usd" in e for e in errors))

    def test_validator_rejects_bool_token_metrics(self):
        errors = validate_trajectory({
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "x", "version": "1"},
            "steps": [
                {
                    "step_id": 1,
                    "source": "agent",
                    "message": "hi",
                    "metrics": {"prompt_tokens": True},
                }
            ],
            "final_metrics": {"total_steps": True, "total_prompt_tokens": True},
        })
        self.assertTrue(any("prompt_tokens" in e for e in errors), errors)
        self.assertTrue(any("total_steps" in e for e in errors), errors)
        self.assertTrue(any("total_prompt_tokens" in e for e in errors), errors)

    def test_validator_rejects_malformed_content_parts(self):
        errors = validate_trajectory({
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "x", "version": "1"},
            "steps": [{"step_id": 1, "source": "user", "message": [123]}],
        })
        self.assertTrue(any("content part" in e for e in errors))

    def test_validator_rejects_falsy_non_object_final_metrics(self):
        for bad_final_metrics in ([], "", 0, False):
            with self.subTest(bad_final_metrics=bad_final_metrics):
                errors = validate_trajectory({
                    "schema_version": "ATIF-v1.7",
                    "agent": {"name": "x", "version": "1"},
                    "steps": [{"step_id": 1, "source": "agent", "message": "hi"}],
                    "final_metrics": bad_final_metrics,
                })
                self.assertTrue(any("final_metrics" in e for e in errors), errors)

    def test_validator_rejects_falsy_non_array_tool_calls(self):
        for bad_tool_calls in ({}, "", 0, False):
            with self.subTest(bad_tool_calls=bad_tool_calls):
                errors = validate_trajectory({
                    "schema_version": "ATIF-v1.7",
                    "agent": {"name": "x", "version": "1"},
                    "steps": [
                        {
                            "step_id": 1,
                            "source": "agent",
                            "message": "hi",
                            "tool_calls": bad_tool_calls,
                        }
                    ],
                })
                self.assertTrue(any("tool_calls" in e for e in errors), errors)

    def test_validator_rejects_unknown_observation_result_fields(self):
        errors = validate_trajectory({
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "x", "version": "1"},
            "steps": [
                {
                    "step_id": 1,
                    "source": "agent",
                    "message": "hi",
                    "tool_calls": [
                        {"tool_call_id": "call-1", "function_name": "bash", "arguments": {}}
                    ],
                    "observation": {
                        "unexpected": True,
                        "results": [
                            {"source_call_id": "call-1", "content": "ok", "status": "success"}
                        ],
                    },
                }
            ],
        })
        self.assertTrue(any("observation" in e and "unknown" in e for e in errors), errors)
        self.assertTrue(any("results[0]" in e and "unknown" in e for e in errors), errors)

    def test_malformed_transcript_json_is_rejected(self):
        path = self._write("codex_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript codex:terminal-bench/task:model:trial1",
            "# harness=codex model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            "{bad json",
        ]))
        with self.assertRaises(ValueError):
            convert_file(path)

    def test_codex_transcript_conversion(self):
        path = self._write("codex_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript codex:terminal-bench/task:model:trial1",
            "# harness=codex model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "hello"}}),
            json.dumps({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "echo ok", "status": "in_progress"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "echo ok", "aggregated_output": "ok\n", "exit_code": 0, "status": "completed"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 15, "cached_input_tokens": 4, "output_tokens": 3, "reasoning_output_tokens": 1}}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual(traj["agent"]["name"], "codex")
        self.assertEqual(traj["final_metrics"]["total_prompt_tokens"], 15)
        self.assertEqual(traj["final_metrics"]["total_cached_tokens"], 4)
        self.assertEqual(traj["final_metrics"]["total_completion_tokens"], 3)

    def test_codex_cumulative_multi_turn_usage_uses_deltas(self):
        path = self._write("codex_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript codex:terminal-bench/task:model:trial1",
            "# harness=codex model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "first"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 1}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "second"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 4, "output_tokens": 6, "reasoning_output_tokens": 2}}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual(traj["final_metrics"]["total_prompt_tokens"], 20)
        self.assertEqual(traj["final_metrics"]["total_cached_tokens"], 4)
        self.assertEqual(traj["final_metrics"]["total_completion_tokens"], 6)
        self.assertEqual(traj["final_metrics"]["extra"]["reasoning_tokens"], 2)

    def test_codex_incomplete_tool_is_not_dropped(self):
        path = self._write("codex_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript codex:terminal-bench/task:model:trial1",
            "# harness=codex model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "sleep 10", "status": "in_progress"}}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        result = traj["steps"][0]["observation"]["results"][0]
        self.assertNotIn("status", result)
        self.assertEqual(result["extra"]["status"], "incomplete")
        self.assertTrue(result["extra"]["incomplete"])

    def test_pi_transcript_conversion(self):
        usage = {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 1, "reasoning": 3, "totalTokens": 18, "cost": {"total": 0.25}}
        path = self._write("pi_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript pi:terminal-bench/task:model:trial1",
            "# harness=pi model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "session", "version": 3, "id": "sess", "cwd": "/work"}),
            json.dumps({"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "plan"}, {"type": "text", "text": "done"}], "model": "model", "usage": usage}}),
            json.dumps({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "bash", "args": {"command": "echo ok"}}),
            json.dumps({"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "bash", "result": {"content": [{"type": "text", "text": "ok"}]}, "isError": False}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual(traj["agent"]["name"], "pi")
        self.assertEqual(traj["final_metrics"]["total_prompt_tokens"], 13)
        self.assertEqual(traj["final_metrics"]["total_cached_tokens"], 2)
        self.assertEqual(traj["final_metrics"]["extra"]["cache_write_tokens"], 1)
        self.assertEqual(traj["final_metrics"]["extra"]["reasoning_tokens"], 3)

    def test_pi_preserves_tool_call_observation_order(self):
        usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "totalTokens": 2, "cost": {"total": 0.01}}
        path = self._write("pi_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript pi:terminal-bench/task:model:trial1",
            "# harness=pi model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "session", "version": 3, "id": "sess", "cwd": "/work"}),
            json.dumps({"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "call-1", "name": "bash", "arguments": {"command": "echo ok"}}], "model": "model", "usage": usage}}),
            json.dumps({"type": "message_end", "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "bash", "content": [{"type": "text", "text": "ok"}], "isError": False}}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}], "model": "model", "usage": usage}}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual([step["source"] for step in traj["steps"]], ["user", "agent", "agent"])
        self.assertEqual(traj["steps"][1]["tool_calls"][0]["tool_call_id"], "call-1")
        self.assertEqual(traj["steps"][1]["observation"]["results"][0]["content"], "ok")
        self.assertEqual(traj["steps"][2]["message"], "done")

    def test_pi_standalone_tool_events_keep_event_order(self):
        usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "totalTokens": 2, "cost": {"total": 0.01}}
        path = self._write("pi_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript pi:terminal-bench/task:model:trial1",
            "# harness=pi model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "session", "version": 3, "id": "sess", "cwd": "/work"}),
            json.dumps({"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}}),
            json.dumps({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "bash", "args": {"command": "echo ok"}}),
            json.dumps({"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "bash", "result": {"content": [{"type": "text", "text": "ok"}]}, "isError": False}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}], "model": "model", "usage": usage}}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual([step["source"] for step in traj["steps"]], ["user", "agent", "agent"])
        self.assertEqual(traj["steps"][1]["tool_calls"][0]["tool_call_id"], "call-1")
        self.assertEqual(traj["steps"][1]["observation"]["results"][0]["content"], "ok")
        self.assertEqual(traj["steps"][2]["message"], "done")

    def test_validate_only_does_not_create_out_dir(self):
        path = self._write("codex_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript codex:terminal-bench/task:model:trial1",
            "# harness=codex model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "hello"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0}}),
        ]))
        out_dir = Path(self.tmp.name) / "should-not-exist"
        self.assertEqual(atif_main(["--validate-only", "--out", str(out_dir), str(path)]), 0)
        self.assertFalse(out_dir.exists())

    def test_pi_sparse_turn_usage_stays_positionally_aligned(self):
        message_usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "totalTokens": 2, "cost": {"total": 0.01}}
        later_turn_usage = {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 1, "reasoning": 3, "totalTokens": 18, "cost": {"total": 0.25}}
        path = self._write("pi_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript pi:terminal-bench/task:model:trial1",
            "# harness=pi model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "session", "version": 3, "id": "sess", "cwd": "/work"}),
            json.dumps({"type": "turn_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "first"}]}}),
            json.dumps({"type": "turn_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "second"}], "usage": later_turn_usage}}),
            json.dumps({"type": "agent_end", "messages": [
                {"role": "user", "content": [{"type": "text", "text": "do it"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "first"}], "model": "model", "usage": message_usage},
                {"role": "assistant", "content": [{"type": "text", "text": "second"}], "model": "model", "usage": message_usage},
            ]}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertNotIn("metrics", traj["steps"][1])
        self.assertEqual(traj["steps"][2]["metrics"]["prompt_tokens"], 13)
        self.assertEqual(traj["final_metrics"]["total_prompt_tokens"], 13)

    def test_pi_prefers_agent_end_messages_and_turn_end_usage(self):
        message_usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "totalTokens": 2, "cost": {"total": 0.01}}
        turn_usage = {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 1, "reasoning": 3, "totalTokens": 18, "cost": {"total": 0.25}}
        path = self._write("pi_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript pi:terminal-bench/task:model:trial1",
            "# harness=pi model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "session", "version": 3, "id": "sess", "cwd": "/work"}),
            json.dumps({"type": "turn_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}], "usage": turn_usage}}),
            json.dumps({"type": "agent_end", "messages": [
                {"role": "user", "content": [{"type": "text", "text": "do it"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "done"}], "model": "model", "usage": message_usage},
            ]}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual([step["source"] for step in traj["steps"]], ["user", "agent"])
        self.assertEqual(traj["final_metrics"]["total_prompt_tokens"], 13)
        self.assertEqual(traj["final_metrics"]["total_completion_tokens"], 5)
        self.assertEqual(traj["final_metrics"]["extra"]["reasoning_tokens"], 3)

    def test_pi_incomplete_tool_is_not_marked_successful(self):
        path = self._write("pi_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript pi:terminal-bench/task:model:trial1",
            "# harness=pi model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps({"type": "session", "version": 3, "id": "sess", "cwd": "/work"}),
            json.dumps({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "bash", "args": {"command": "sleep 10"}}),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        result = traj["steps"][0]["observation"]["results"][0]
        self.assertNotIn("status", result)
        self.assertEqual(result["extra"]["status"], "incomplete")
        self.assertTrue(result["extra"]["incomplete"])

    def test_claude_transcript_conversion(self):
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 2,
            "result": "finished",
            "usage": {"input_tokens": 7, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1, "output_tokens": 4},
        }
        path = self._write("claude_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript claude:terminal-bench/task:model:trial1",
            "# harness=claude model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps(result),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual(traj["agent"]["name"], "claude")
        self.assertEqual(traj["steps"][0]["message"], "finished")
        self.assertEqual(traj["final_metrics"]["total_prompt_tokens"], 10)
        self.assertEqual(traj["final_metrics"]["total_cached_tokens"], 2)
        self.assertEqual(traj["final_metrics"]["extra"]["cache_write_tokens"], 1)

    def test_claude_model_usage_preserves_cost(self):
        result = {
            "type": "result",
            "result": "finished",
            "modelUsage": {
                "model-a": {
                    "inputTokens": 7,
                    "cacheReadInputTokens": 2,
                    "cacheCreationInputTokens": 1,
                    "outputTokens": 4,
                    "costUSD": 0.5,
                }
            },
        }
        path = self._write("claude_terminal-bench_task_model_trial1.txt", "\n".join([
            "# transcript claude:terminal-bench/task:model:trial1",
            "# harness=claude model=model task=terminal-bench/task trial=1 ts=2026-01-01T00:00:00",
            "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.",
            "",
            json.dumps(result),
        ]))
        traj = convert_file(path)
        self.assertEqual(validate_trajectory(traj), [])
        self.assertEqual(traj["steps"][0]["metrics"]["cost_usd"], 0.5)
        self.assertEqual(traj["final_metrics"]["total_cost_usd"], 0.5)


if __name__ == "__main__":
    unittest.main()

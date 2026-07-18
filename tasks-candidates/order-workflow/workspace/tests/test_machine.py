"""Tests for the generic StateMachine engine."""
import unittest

from workflow.machine import StateMachine, InvalidTransition


class MachineTest(unittest.TestCase):
    def test_basic_transition(self):
        m = StateMachine("A")
        m.add_transition("A", "go", "B")
        m.fire("go")
        self.assertEqual(m.current_state, "B")

    def test_invalid_event_raises(self):
        m = StateMachine("A")
        m.add_transition("A", "go", "B")
        with self.assertRaises(InvalidTransition):
            m.fire("nope")
        self.assertEqual(m.current_state, "A")

    def test_guard_pass_and_reject(self):
        m = StateMachine("A")
        m.add_transition("A", "go", "B", guard=lambda mm, ok=False, **k: ok)
        # Guard rejects: stay put, and it must raise.
        with self.assertRaises(InvalidTransition):
            m.fire("go", ok=False)
        self.assertEqual(m.current_state, "A")
        # Guard passes: move.
        m.fire("go", ok=True)
        self.assertEqual(m.current_state, "B")

    def test_first_passing_guard_wins(self):
        m = StateMachine("A")
        m.add_transition("A", "go", "X", guard=lambda mm, **k: False)
        m.add_transition("A", "go", "Y", guard=lambda mm, **k: True)
        m.add_transition("A", "go", "Z", guard=lambda mm, **k: True)
        m.fire("go")
        # Y is the first candidate whose guard passes, even though Z would too.
        self.assertEqual(m.current_state, "Y")

    def test_entry_action_runs(self):
        log = []
        m = StateMachine("A")
        m.add_state("B", on_entry=lambda mm: log.append("enter B"))
        m.add_transition("A", "go", "B")
        m.fire("go")
        self.assertEqual(log, ["enter B"])

    def test_exit_action_runs(self):
        log = []
        m = StateMachine("A")
        m.add_state("A", on_exit=lambda mm: log.append("exit A"))
        m.add_transition("A", "go", "B")
        m.fire("go")
        self.assertEqual(log, ["exit A"])

    def test_exit_then_entry_order(self):
        log = []
        m = StateMachine("A")
        m.add_state("A", on_exit=lambda mm: log.append("exit A"))
        m.add_state("B", on_entry=lambda mm: log.append("enter B"))
        m.add_transition("A", "go", "B")
        m.fire("go")
        self.assertEqual(log, ["exit A", "enter B"])

    def test_reentrant_nested_event(self):
        # B's entry action fires a nested event; it must be evaluated against
        # B (the just-entered state), landing the machine in C.
        m = StateMachine("A")
        m.add_state("B", on_entry=lambda mm: mm.fire("next"))
        m.add_transition("A", "go", "B")
        m.add_transition("B", "next", "C")
        m.fire("go")
        self.assertEqual(m.current_state, "C")

    def test_run_to_completion_order(self):
        # A nested event raised inside an entry action must be deferred until
        # that action has fully returned (run-to-completion), so the log reads
        # B_start, B_end, C_start -- not B_start, C_start, B_end.
        log = []

        def b_entry(mm):
            log.append("B_start")
            mm.fire("hop")
            log.append("B_end")

        m = StateMachine("A")
        m.add_state("B", on_entry=b_entry)
        m.add_state("C", on_entry=lambda mm: log.append("C_start"))
        m.add_transition("A", "go", "B")
        m.add_transition("B", "hop", "C")
        m.fire("go")
        self.assertEqual(log, ["B_start", "B_end", "C_start"])
        self.assertEqual(m.current_state, "C")

    def test_history_records_transitions(self):
        m = StateMachine("A")
        m.add_transition("A", "go", "B")
        m.add_transition("B", "go2", "C")
        m.fire("go")
        m.fire("go2")
        self.assertEqual(m.history.path(), ["B", "C"])
        self.assertEqual(m.history.current(), "C")
        self.assertTrue(m.history.was_visited("B"))
        # The initial state is never "transitioned into", so it is not logged.
        self.assertFalse(m.history.was_visited("A"))


if __name__ == "__main__":
    unittest.main()

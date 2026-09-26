import unittest

from taskflow.model import JobRun, State, Task
from taskflow.statemachine import StateMachine


class StateMachineTest(unittest.TestCase):
    def _machine(self):
        return StateMachine(JobRun(Task("a")))

    def test_legal_lifecycle(self):
        sm = self._machine()
        self.assertEqual(sm.state, State.PENDING)
        sm.advance(State.READY, now=1)
        sm.advance(State.RUNNING, now=2)
        sm.advance(State.SUCCEEDED, now=3)
        self.assertEqual(sm.state, State.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()

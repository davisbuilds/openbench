"""Workflow engine package: a generic state machine plus an order workflow."""

from workflow.machine import StateMachine, State, InvalidTransition
from workflow.transitions import Transition, TransitionTable
from workflow.history import History

__all__ = [
    "StateMachine",
    "State",
    "InvalidTransition",
    "Transition",
    "TransitionTable",
    "History",
]

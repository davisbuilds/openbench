"""Tests for the concrete order-fulfillment workflow."""
import unittest

from workflow.machine import InvalidTransition
from workflow import order_workflow
from workflow.order_workflow import Order


class OrderWorkflowTest(unittest.TestCase):
    def test_happy_path_to_delivered(self):
        order = Order(stock=5)
        m = order_workflow.build(order)
        # Paying auto-reserves stock and advances straight to PACKED.
        m.fire("pay", amount=100)
        self.assertEqual(m.current_state, "PACKED")
        m.fire("ship")
        m.fire("deliver")
        self.assertEqual(m.current_state, "DELIVERED")
        self.assertFalse(order.backorder)
        self.assertEqual(
            m.history.path(), ["PAID", "PACKED", "SHIPPED", "DELIVERED"]
        )

    def test_pay_requires_positive_amount(self):
        order = Order(stock=5)
        m = order_workflow.build(order)
        with self.assertRaises(InvalidTransition):
            m.fire("pay", amount=0)
        self.assertEqual(m.current_state, "CREATED")

    def test_cancel_branches(self):
        # Cancel from CREATED.
        m1 = order_workflow.build(Order(stock=5))
        m1.fire("cancel")
        self.assertEqual(m1.current_state, "CANCELLED")

        # Cancel from PAID (reachable while backordered, i.e. no stock).
        order = Order(stock=0)
        m2 = order_workflow.build(order)
        m2.fire("pay", amount=25)
        self.assertEqual(m2.current_state, "PAID")
        m2.fire("cancel")
        self.assertEqual(m2.current_state, "CANCELLED")

    def test_backorder_when_no_stock(self):
        order = Order(stock=0)
        m = order_workflow.build(order)
        m.fire("pay", amount=40)
        # No stock: stays in PAID and is flagged backordered; never packed.
        self.assertEqual(m.current_state, "PAID")
        self.assertTrue(order.backorder)
        self.assertFalse(m.history.was_visited("PACKED"))

    def test_rejected_transition_no_side_effect(self):
        order = Order(stock=5)
        m = order_workflow.build(order)
        with self.assertRaises(InvalidTransition):
            m.fire("pay", amount=0)
        # A rejected transition must not run any entry/exit action.
        self.assertEqual(order.audit, [])

    def test_cancel_after_rejected_pay_audit_consistent(self):
        order = Order(stock=5)
        m = order_workflow.build(order)
        try:
            m.fire("pay", amount=0)
        except InvalidTransition:
            pass
        # The rejected pay left no trace; the subsequent valid cancel is the
        # only thing in the audit trail.
        m.fire("cancel")
        self.assertEqual(m.current_state, "CANCELLED")
        self.assertEqual(order.audit, [("exit", "CREATED"), ("entry", "CANCELLED")])

    def test_history_excludes_rejected(self):
        order = Order(stock=5)
        m = order_workflow.build(order)
        try:
            m.fire("pay", amount=0)
        except InvalidTransition:
            pass
        # A guard-rejected pay must not be logged as a visit to PAID.
        self.assertFalse(m.history.was_visited("PAID"))
        self.assertEqual(m.history.path(), [])


if __name__ == "__main__":
    unittest.main()

"""A concrete order-fulfillment workflow built on the generic engine.

Happy path::

    CREATED --pay--> PAID --reserve_stock--> PACKED --ship--> SHIPPED
            --deliver--> DELIVERED

Side branches:

* ``cancel`` moves CREATED or PAID to CANCELLED.
* ``pay`` is guarded: the order can only be paid when the payment ``amount``
  passed with the event is positive.
* Arriving in PAID auto-raises a ``reserve_stock`` event. If stock is
  available the order advances to PACKED; otherwise it stays in PAID and is
  flagged as backordered. The auto-reserve fires once per order.

Every state's entry/exit action appends to ``order.audit`` so the exact
sequence of state changes is observable -- which is how a mis-timed action
(one that fires on a rejected transition) becomes visible.
"""

from workflow.machine import StateMachine


class Order:
    """Mutable context object threaded through the workflow."""

    def __init__(self, stock=1):
        # Units of stock on hand; > 0 means the order can be packed.
        self.stock = stock
        self.backorder = False
        # Guards the one-shot auto reserve so a self-loop back into PAID does
        # not re-trigger it forever.
        self.reserve_attempted = False
        # Ordered log of ('entry'|'exit', state_name) as actions run.
        self.audit = []


def _exit_action(name):
    def action(machine):
        machine.context.audit.append(("exit", name))
    return action


def _entry_action(name):
    def action(machine):
        machine.context.audit.append(("entry", name))
    return action


def _paid_entry(machine):
    """Entry action for PAID: audit, then auto-raise reserve_stock once."""
    order = machine.context
    order.audit.append(("entry", "PAID"))
    if not order.reserve_attempted:
        order.reserve_attempted = True
        machine.fire("reserve_stock")


def _flag_backorder(machine):
    machine.context.backorder = True


def _amount_is_positive(machine, amount=0, **_kwargs):
    return amount > 0


def _has_stock(machine, **_kwargs):
    return machine.context.stock > 0


def _out_of_stock(machine, **_kwargs):
    return machine.context.stock <= 0


def build(order=None):
    """Construct and return a :class:`StateMachine` for one order."""
    if order is None:
        order = Order()

    machine = StateMachine("CREATED")
    machine.context = order

    machine.add_state("CREATED", on_entry=_entry_action("CREATED"),
                      on_exit=_exit_action("CREATED"))
    machine.add_state("PAID", on_entry=_paid_entry, on_exit=_exit_action("PAID"))
    machine.add_state("PACKED", on_entry=_entry_action("PACKED"),
                      on_exit=_exit_action("PACKED"))
    machine.add_state("SHIPPED", on_entry=_entry_action("SHIPPED"),
                      on_exit=_exit_action("SHIPPED"))
    machine.add_state("DELIVERED", on_entry=_entry_action("DELIVERED"),
                      on_exit=_exit_action("DELIVERED"))
    machine.add_state("CANCELLED", on_entry=_entry_action("CANCELLED"),
                      on_exit=_exit_action("CANCELLED"))

    machine.add_transition("CREATED", "pay", "PAID", guard=_amount_is_positive)
    machine.add_transition("CREATED", "cancel", "CANCELLED")
    machine.add_transition("PAID", "cancel", "CANCELLED")
    # Two transitions share (PAID, reserve_stock); insertion order decides
    # which guard is consulted first.
    machine.add_transition("PAID", "reserve_stock", "PACKED", guard=_has_stock)
    machine.add_transition("PAID", "reserve_stock", "PAID", guard=_out_of_stock,
                           action=_flag_backorder)
    machine.add_transition("PACKED", "ship", "SHIPPED")
    machine.add_transition("SHIPPED", "deliver", "DELIVERED")

    return machine

"""Late-return fee calculation."""

DAILY_FEE = 0.25


def late_fee(days_late):
    """Fee owed for returning a book ``days_late`` days after its due date.

    The fee accrues at ``DAILY_FEE`` per day late; on-time or early returns owe
    nothing.
    """
    if days_late <= 0:
        return 0.0
    return round(days_late + DAILY_FEE, 2)

"""Explicit Codex model identities shared by adapters and the repair lane.

Codex metadata lists low through max for Sol/Luna. Ultra delegates tasks and
is intentionally outside the single-agent benchmark contract. Bare defaults
match Codex metadata; campaigns should select an explicit effort alias.
"""

SOL_LUNA_DEFAULTS = {"gpt-6.1-sol": "low", "gpt-6-sol": "low", "gpt-6-luna": "medium"}
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max")
SOL_LUNA_PAIRS = {
    **{model: (model, effort) for model, effort in SOL_LUNA_DEFAULTS.items()},
    **{f"{model}-{effort}": (model, effort)
       for model in SOL_LUNA_DEFAULTS for effort in CODEX_EFFORTS},
}
# Supported configuration does not imply runtime admission. Each selected
# model/effort still requires matching authenticated control evidence.
REPAIR_MODEL_PAIRS = {
    "gpt-5.6-terra-xhigh": ("gpt-5.6-terra", "xhigh"),
    "gpt-5.6-luna-max": ("gpt-5.6-luna", "max"),
    **SOL_LUNA_PAIRS,
}

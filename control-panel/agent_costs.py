"""What an agent costs to run, and how many commands a budget buys. Pure logic; app.py enforces the result.

Prices are dollars per million tokens (input, output), the same as the model menu in app.py. A lab command is one
model call, and its input is the whole conversation so far, so the input tokens per command are an estimate. It is not
measured: the one live run that checked it (one member, 112 commands on Haiku) would cost about $1.50 at this
estimate, which is in the range the provider's usage page should show. Check the console for the real spend.
"""

PRICES = {                                   # model id -> ($ per M input tokens, $ per M output tokens)
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
INPUT_TOKENS_PER_COMMAND = 12_000            # the conversation so far, on average, for one command's model call
OUTPUT_TOKENS_PER_COMMAND = 300
MIN_COMMANDS = 5                             # below this a budget can't run a useful turn


def model_name(model_id):
    """'anthropic/claude-haiku-4-5' -> 'claude-haiku-4-5'."""
    return (model_id or "").split("/", 1)[-1]


def price_for(model_id):
    """(input, output) dollars per million tokens, or None if the model has no price here."""
    return PRICES.get(model_name(model_id))


def cost_per_command(model_id):
    """Estimated dollars for one command on this model, or None if the price is unknown."""
    p = price_for(model_id)
    if p is None:
        return None
    return (INPUT_TOKENS_PER_COMMAND * p[0] + OUTPUT_TOKENS_PER_COMMAND * p[1]) / 1_000_000


def commands_for_budget(budget_usd, model_id):
    """How many commands `budget_usd` buys on this model. Raises ValueError with what to change if it buys fewer
    than MIN_COMMANDS, or if the model has no known price (the budget can't be enforced without one)."""
    per = cost_per_command(model_id)
    if per is None:
        raise ValueError(f"no price is known for {model_id or 'this model'}, so a budget can't be enforced for it")
    n = int(budget_usd // per)
    if n < MIN_COMMANDS:
        raise ValueError(f"${budget_usd:.2f} buys only {n} commands on {model_name(model_id)} "
                         f"(about ${per:.3f} each); raise the budget or use a cheaper model")
    return n

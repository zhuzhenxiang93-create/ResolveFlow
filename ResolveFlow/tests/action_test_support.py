"""Legacy execution tests simulate explicit user consent, never a production bypass."""
from agents.action_runtime import ActionRuntime as ProductionRuntime
from agents.goal_interpreter import GoalInterpreter
from core.auth import mint_token as _mint_token


def mint_token(role="user", subject="test-user", ttl_seconds=3600):
    """Test convenience: AGENT_JWT_SECRET must already be set (e.g. via
    patch.dict("os.environ", ...)) before calling this."""
    return _mint_token(subject, role, ttl_seconds)


class ActionRuntime(ProductionRuntime):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("goal_interpreter", GoalInterpreter())
        super().__init__(*args, **kwargs)

    async def advance(self, *args, **kwargs):
        task = await super().advance(*args, **kwargs)
        for _ in range(4):
            if task["status"] != "awaiting_confirmation":
                break
            # Independent goals can each have their own pending confirmation
            # at once now; accept all of them before re-entering the
            # scheduler, not just one.
            for c in list(task["confirmations"].values()):
                if c["status"] == "pending":
                    self.confirm(task["id"], task["owner"], c["id"], True)
            task = await super().advance(task["id"], task["owner"])
        return task

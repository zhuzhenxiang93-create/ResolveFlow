"""Local convenience CLI: mint a role-scoped JWT for curl/Swagger testing.

Reads AGENT_JWT_SECRET from .env.agent.local or the process environment —
same-machine access to that secret is the existing trust boundary (see
wiki/agent-execution.md), this just saves a manual jwt.encode() call.
Does not talk to the server; it signs locally with the same key the server
verifies against, so the server must be configured with the same secret.
"""
import argparse
import os
from pathlib import Path

from dotenv import dotenv_values

from core.auth import ROLES, mint_token

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subject", help="任意用户/审核员标识，例如 alice 或 reviewer-1")
    parser.add_argument("role", choices=sorted(ROLES))
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args()
    cfg = {**dotenv_values(ROOT / ".env.agent.local"), **os.environ}
    secret = cfg.get("AGENT_JWT_SECRET")
    if not secret:
        raise SystemExit("请在 .env.agent.local 或环境变量中配置 AGENT_JWT_SECRET（须与服务端一致）")
    os.environ["AGENT_JWT_SECRET"] = secret
    token = mint_token(args.subject, args.role, args.ttl_seconds)
    print(token)


if __name__ == "__main__":
    main()

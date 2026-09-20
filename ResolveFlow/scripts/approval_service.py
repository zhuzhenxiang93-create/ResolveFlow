"""Loopback approval service; JWT signing/admin-bootstrap secrets are local-only."""
import argparse
import os
import secrets
import stat
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / ".env.approval.local"


def credentials():
    try:
        fd = os.open(str(AUTH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write("AGENT_JWT_SECRET=" + secrets.token_hex(32) + "\nAGENT_ADMIN_SECRET=" + secrets.token_hex(32) + "\n")
    if AUTH.is_symlink() or stat.S_IMODE(AUTH.stat().st_mode) != 0o600:
        raise ValueError("Approval credential file must be a regular private file (chmod 600)")
    values = dotenv_values(AUTH)
    if not values.get("AGENT_JWT_SECRET") or not values.get("AGENT_ADMIN_SECRET"):
        raise ValueError("JWT signing secret and admin bootstrap secret both required")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    auth = credentials()
    if args.init_only:
        print("Private role credentials ready; no keys displayed, no server started.")
        return
    load_dotenv(ROOT / ".env.agent.local")
    os.environ.update({key: auth[key] for key in ("AGENT_JWT_SECRET", "AGENT_ADMIN_SECRET")})
    import uvicorn
    print("Local simulated reviewer service. Release/approval can resume model execution and incur API usage.")
    uvicorn.run("api.action_demo:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

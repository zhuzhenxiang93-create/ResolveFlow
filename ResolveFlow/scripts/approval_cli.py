"""Reviewer-only HTTP client; never edits SQLite or grants user consent."""
import argparse
import json
import os
from urllib.parse import urlparse

import httpx
from scripts.approval_service import AUTH
from dotenv import dotenv_values
from scripts.task_view import summary

from core.auth import mint_token

REVIEWER_SUBJECT = "local-reviewer"

HELP = """/list                         最近100个待审批或需人工处理任务
/show [任务ID]                查看或刷新当前选中的任务摘要
/debug                       查看当前任务完整 JSON（只读）
/approve 审批ID               批准已查看的具体申请（需再次输入确认）
/reject 审批ID                拒绝已查看的具体申请
/release 任务ID               恢复已查看的人工作业，旧授权失效，需用户重新确认
/quit                         退出
本入口模拟独立审核角色；同一台机器的所有者仍能访问本地凭据，不是生产权限隔离。"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    url = urlparse(args.url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"} or url.username or url.password:
        raise SystemExit("Only loopback HTTP is allowed for local demo credentials")
    if AUTH.is_symlink() or not AUTH.exists() or AUTH.stat().st_mode & 0o077:
        raise SystemExit("Run make approval-init first; credentials must be private")
    jwt_secret = dotenv_values(AUTH).get("AGENT_JWT_SECRET")
    if not jwt_secret:
        raise SystemExit("Missing JWT signing secret; no request sent")
    # Same-machine file access to the signing secret is the existing trust
    # boundary (see the module docstring below) — minting locally avoids a
    # network round trip through the admin-bootstrap endpoint for a CLI that
    # already has the key.
    os.environ["AGENT_JWT_SECRET"] = jwt_secret
    token = mint_token(REVIEWER_SUBJECT, "reviewer", ttl_seconds=3600)
    selected = None
    print(HELP)
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer " + token}, timeout=120, follow_redirects=False) as client:
        while True:
            try:
                line = input("\n审核员> ").strip()
                if line == "/quit":
                    break
                command, _, ident = line.partition(" ")
                ident = ident.strip()
                if command == "/list":
                    response = client.get("/agent/review/tasks")
                elif command in {"/show", "/debug"}:
                    ident = ident or (selected["id"] if selected else "")
                    if not ident:
                        print("尚未选中任务，请先 /show 任务ID。")
                        continue
                    selected = None
                    response = client.get("/agent/review/tasks/" + ident)
                    if response.is_success:
                        selected = response.json()["task"]
                elif command in {"/approve", "/reject", "/release"} and selected:
                    task_id = selected["id"]
                    if command == "/release":
                        if ident != task_id or selected["status"] != "needs_human":
                            print("请先 /show 要恢复的 needs_human 任务。")
                            continue
                        path, body = "/release", None
                    else:
                        # Independent goals can each have their own pending
                        # approval now; accept the id if it matches ANY of
                        # them, not just a single assumed approval.
                        approval_ids = {a["id"] for a in (selected.get("approvals") or {}).values()}
                        if selected["status"] != "awaiting_approval" or ident not in approval_ids:
                            print("编号不匹配或任务不在待审批状态；请重新 /show。")
                            continue
                        path, body = "/approval", {"approval_id": ident, "approved": command == "/approve"}
                    print("将执行", command, "对象", ident, "；可能恢复模型调用。")
                    if input("核对证据后，输入 YES 执行：").strip() != "YES":
                        print("未执行。")
                        continue
                    response = client.post("/agent/tasks/" + task_id + path, json=body)
                else:
                    print(HELP)
                    continue
                if response.is_redirect:
                    print("已拒绝重定向，未转发凭据。")
                    continue
                payload = response.json()
                if response.is_success and "status" in payload and "id" in payload:
                    selected = payload
                print(json.dumps(payload, ensure_ascii=False, indent=2) if command == "/debug" else summary(payload, reviewer=True))
            except (EOFError, KeyboardInterrupt):
                break
            except httpx.TransportError:
                selected = None
                print("连接失败或响应不确定。不要盲目重复审批；先 /show 查看服务器状态。")
            except ValueError:
                selected = None
                print("无法解析响应，请检查本地审批服务。")


if __name__ == "__main__":
    main()

"""Local interactive client for the shared action orchestrator."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import dotenv_values

from agents.action_runtime import ActionRuntime, TERMINAL
from agents.agent_orchestrator import AgentOrchestrator
from core.llm_client import LLMClient
from scripts.task_view import summary

ROOT = Path(__file__).resolve().parents[1]
HELP = """直接输入问题或补充信息。
/seed                  创建一笔模拟订单（不调用模型）
/new 问题              创建新任务，不取消旧任务
/resume 任务ID         加载已有任务（不自动执行）
/status                查看当前任务
/debug                 查看最近会话诊断或当前任务 JSON
/session               显示当前会话编号（用于跨进程继续）
/conversation 会话ID    恢复会话，不自动执行操作
/confirm 确认ID        同意当前具体修改并继续
/reject 确认ID         拒绝修改
/continue              继续当前任务或刷新审批结果
/revise 新目标         取消旧任务并以新目标开始
/cancel                取消当前任务
/help                  显示帮助
/quit                  退出，保留任务
人工审批须由独立审批入口处理，本 CLI 不提供自我审批。"""


class Session:
    def __init__(self, runtime, owner="demo-user", unified=False):
        self.runtime, self.owner, self.task = runtime, owner, None
        self.conversation_id = None
        self.last_result = None
        self.service = None
        if unified:
            from api.action_routes import conversation_service_for
            self.service = conversation_service_for(runtime)

    async def execute(self, **kwargs):
        self.task = await AgentOrchestrator.execute_action(
            self.runtime, owner=self.owner, **kwargs)
        if self.service:
            self.conversation_id = self.task["conversation_id"]
            await self.service.observe(self.task)
        return self.task

    async def handle(self, line):
        line = line.strip()
        if not line:
            return None
        command, _, argument = line.partition(" ")
        argument = argument.strip()
        if command == "/help":
            return HELP
        if command == "/session":
            return self.conversation_id or "尚未开始会话"
        if command == "/conversation" and self.service:
            if not argument:
                raise ValueError("请提供会话ID")
            self.conversation_id, task_id, _ = self.service._conversation(self.owner, argument)
            self.task = self.runtime.get(task_id, self.owner) if task_id else None
            return self.task or "会话已恢复，请继续输入问题。"
        if command == "/debug":
            if self.service and self.last_result is not None:
                return json.dumps({k: self.last_result.get(k) for k in ("conversation_id", "route", "status", "interpretation", "degradations")}, ensure_ascii=False, indent=2)
            if not self.task:
                raise ValueError("当前没有任务")
            self.task = self.runtime.get(self.task["id"], self.owner)
            return json.dumps(self.task, ensure_ascii=False, indent=2)
        if command == "/seed":
            return {"simulated_order": self.runtime.seed(self.owner)}
        if command == "/resume":
            self.task = self.runtime.get(argument, self.owner)
            if self.service:
                self.conversation_id = self.task["conversation_id"]
                await self.service.observe(self.task)
            return self.task
        if command == "/new":
            if not argument:
                raise ValueError("请输入新任务的问题")
            if self.service:
                return await self.converse(argument, new_task=True)
            return await self.execute(message=argument)
        if not command.startswith("/"):
            if self.service:
                return await self.converse(line)
            if self.task and self.task["status"] in TERMINAL:
                raise ValueError("当前任务已结束或需人工处理；使用 /new 问题 创建新任务")
            return await self.execute(message=line, task_id=self.task["id"] if self.task else None)
        if command not in {"/status", "/confirm", "/reject", "/continue", "/revise", "/cancel"}:
            raise ValueError("未知命令；输入 /help 查看帮助")
        if not self.task:
            raise ValueError("请先输入问题或 /resume 任务ID")
        task_id = self.task["id"]
        if command == "/status":
            self.task = self.runtime.get(task_id, self.owner)
        elif command == "/cancel":
            self.task = self.runtime.cancel(task_id, self.owner)
        elif command == "/revise":
            if not argument:
                raise ValueError("请输入修正后的完整目标")
            self.task = self.runtime.revise(task_id, self.owner, argument)
            return await self.execute(task_id=self.task["id"], message=argument)
        elif command in {"/confirm", "/reject"}:
            if not argument:
                raise ValueError("必须提供当前显示的确认ID")
            self.task = self.runtime.confirm(task_id, self.owner, argument, command == "/confirm")
            if self.task["status"] == "running":
                return await self.execute(task_id=task_id)
        else:
            return await self.execute(task_id=task_id)
        if self.service and command != "/status":
            await self.service.observe(self.task)
        return self.task

    async def converse(self, message, new_task=False):
        result = await self.service.send(self.owner, message, conversation_id=self.conversation_id, new_task=new_task)
        self.last_result = result
        self.conversation_id = result["conversation_id"]
        self.task = result["task"] or self.task
        return result


def display(value):
    if value is None:
        return
    if isinstance(value, dict) and "route" in value:
        if value.get("task"):
            print("\nAgent:", summary({**value["task"], "response": value["response"]}))
        else:
            print("\nAgent:", value["response"])
        if value["degradations"]:
            print("部分依赖不可用:", ", ".join(value["degradations"]))
        return
    if not isinstance(value, dict) or "status" not in value:
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
        return
    print("\nAgent:", summary(value))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="明确使用离线规则，不调用真实模型")
    parser.add_argument("--resume", help="加载已有任务ID，不自动执行")
    args = parser.parse_args()
    cfg = {**dotenv_values(ROOT / ".env.agent.local"), **os.environ}
    client = None
    if not args.offline:
        if not cfg.get("LLM_API_KEY") or not cfg.get("LLM_MODEL"):
            raise SystemExit("请在 .env.agent.local 配置 LLM_API_KEY 和 LLM_MODEL；不会自动降级。")
        client = LLMClient(api_key=cfg["LLM_API_KEY"], model=cfg["LLM_MODEL"],
                           base_url=cfg.get("LLM_BASE_URL"), provider=cfg.get("LLM_PROVIDER", "openai"), max_retries=0)
    try:
        runtime = ActionRuntime(cfg.get("AGENT_STATE_PATH") or ROOT / "data/agent/state.sqlite3",
                                client=client, allow_fallback=False, model_timeout=30)
        session = Session(runtime, unified=True)
        print("ResolveFlow | " + ("离线规则" if args.offline else "真实模型 " + client.model))
        print("业务系统为本地模拟；本地 CLI 是可信开发入口，不是生产认证接口。")
        print("真实模式每次推进可能调用多次模型并产生费用；退出后不继续后台调用。")
        print("统一会话已启用：可以连续咨询和办理，无需 /new。记忆使用本地 SQLite，知识使用现有关键词检索组件。")
        print(HELP)
        if args.resume:
            display(await session.handle("/resume " + args.resume))
        while True:
            try:
                line = input("\n你> ").strip()
            except EOFError:
                break
            if line == "/quit":
                break
            try:
                display(await session.handle(line))
            except ValueError as ex:
                print("未执行或已暂停:", str(ex))
            except Exception as ex:
                print("执行异常:", type(ex).__name__, "。使用 /status 检查任务，切勿盲目重试修改。")
    finally:
        if client:
            await client._client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。任务保留；中断执行后请先检查状态。")

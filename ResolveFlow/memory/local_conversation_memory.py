"""Local adapter for the existing memory contract, not a Redis/Chroma emulator."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from memory.conversation_memory import MemoryContext, Message, MsgRole
from mcp.hybrid_retriever import BM25Index


class LocalConversationMemory:
    mode = "sqlite_recent_and_lexical_history"

    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversation_messages (id INTEGER PRIMARY KEY, owner TEXT, conversation TEXT, role TEXT, content TEXT, timestamp TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS conversation_profiles (owner TEXT PRIMARY KEY, body TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    async def add_message(self, user_id, conv_id, role, content, metadata=None):
        # Never retain common pasted API keys or credential-like fields as memory.
        text = re.sub(r"sk-[A-Za-z0-9_-]+|(?i:password|密码|验证码)\s*[:：=]\s*\S+", "[REDACTED]", content)
        with self.connect() as db:
            db.execute("INSERT INTO conversation_messages(owner,conversation,role,content,timestamp) VALUES(?,?,?,?,?)",
                       (user_id, conv_id, role.value, text[:8000], datetime.now().isoformat()))

    async def get_context(self, user_id, conv_id, query=""):
        with self.connect() as db:
            recent = db.execute("SELECT role,content,timestamp FROM conversation_messages WHERE owner=? AND conversation=? ORDER BY id DESC LIMIT 12", (user_id, conv_id)).fetchall()
            older = db.execute("SELECT id,content FROM conversation_messages WHERE owner=? AND conversation!=? ORDER BY id DESC LIMIT 100", (user_id, conv_id)).fetchall()
            profile = db.execute("SELECT body FROM conversation_profiles WHERE owner=?", (user_id,)).fetchone()
        index = BM25Index()
        index.upsert([{"chunk_id": str(row[0]), "content": row[1]} for row in older])
        hits = index.search(query, 3)
        return MemoryContext([Message(MsgRole(role), text, datetime.fromisoformat(ts)) for role, text, ts in reversed(recent)],
                             [hit["content"] for hit in hits], json.loads(profile[0]) if profile else {}, "")

    async def update_profile(self, user_id, conv_id):
        context = await self.get_context(user_id, conv_id)
        profile = context.user_profile
        for message in context.recent_messages:
            if message.role != MsgRole.USER:
                continue
            text = message.content
            if re.search(r"请.*(简短|简洁)|我喜欢.*简洁", text):
                profile["response_style"] = "concise"
            if re.search(r"请.*(详细|具体).*解释|我喜欢.*详细", text):
                profile["response_style"] = "detailed"
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO conversation_profiles VALUES(?,?)", (user_id, json.dumps(profile)))

    async def close(self):
        pass

"""Real Redis/Chroma; controlled model barriers test storage concurrency."""
import asyncio
import os
import unittest
import uuid
from memory.conversation_memory import MemoryManager, MsgRole

@unittest.skipUnless(os.getenv('RF_DEPENDENCY_TEST')=='1','requires isolated Redis:16379 and Chroma:18001')
class MemoryDependencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.m=MemoryManager(redis_url='redis://127.0.0.1:16379/0',chroma_host='127.0.0.1',chroma_port=18001,api_key='test')
        self.user='dependency-'+str(uuid.uuid4());await self.m._redis.ping()
    async def asyncTearDown(self):await self.m.close()
    async def test_latest_legacy_profile_and_stable_update(self):
        await asyncio.to_thread(self.m._profile.add,ids=[self.user+'a',self.user+'b'],documents=['{"response_style":"detailed"}','{"response_style":"concise"}'],metadatas=[{'user_id':self.user,'ts':'2025-01-01'},{'user_id':self.user,'ts':'2026-01-01'}])
        self.assertEqual((await self.m.get_profile(self.user))['response_style'],'concise')
        await self.m.set_profile(self.user,{'response_style':'detailed'})
        self.assertEqual((await self.m.get_profile(self.user))['response_style'],'detailed')
        self.assertEqual(await self.m.get_profile(self.user+'foreign'),{})
        await self.m.forget(self.user);self.assertEqual(await self.m.get_profile(self.user),{})
    async def test_compression_preserves_concurrent_message_order(self):
        self.m.COMPRESS_AT=1000
        for i in range(15):await self.m.add_message(self.user,'c',MsgRole.USER,str(i))
        self.m.COMPRESS_AT=15
        entered,release=asyncio.Event(),asyncio.Event()
        async def summarize(old,text):entered.set();await release.wait();return '历史订单和用户偏好摘要'
        self.m._summarize_single_pass=summarize
        task=asyncio.create_task(self.m._compress(self.user,'c'));await entered.wait()
        self.m.COMPRESS_AT=1000;await self.m.add_message(self.user,'c',MsgRole.USER,'NEW');release.set();await task
        messages=await self.m._get_working_memory(self.user,'c')
        self.assertEqual([m.content for m in messages],['10','11','12','13','14','NEW'])
        self.assertTrue((await self.m.get_context(self.user,'another','历史订单')).relevant_history)
        await self.m.forget(self.user)
    async def test_forget_during_profile_generation_prevents_resurrection(self):
        entered,release=asyncio.Event(),asyncio.Event()
        class ControlledModel:
            async def create(self,**kwargs):entered.set();await release.wait();return '{"preferences":["详细"]}'
        self.m._client=ControlledModel();await self.m.add_message(self.user,'c',MsgRole.USER,'我喜欢详细解释')
        messages=await self.m._get_working_memory(self.user,'c')
        task=asyncio.create_task(self.m._run_profile_update(self.user,'c',messages,'0'));await entered.wait()
        await self.m.forget(self.user);release.set();await task
        self.assertEqual(await self.m.get_profile(self.user),{});self.assertFalse((await self.m.get_context(self.user,'c')).recent_messages)
    async def test_late_profile_result_cannot_overwrite_newer_update(self):
        from datetime import datetime
        from memory.conversation_memory import Message
        entered,release=asyncio.Event(),asyncio.Event()
        class ControlledModel:
            async def create(inner,**kwargs):
                if '简洁' in kwargs['messages'][0]['content']:
                    entered.set();await release.wait()
                return '{}'
        self.m._client=ControlledModel()
        self.m._profile_revisions[self.user]=1
        old=asyncio.create_task(self.m._run_profile_update(self.user,'old',[Message(MsgRole.USER,'我喜欢简洁回答',datetime.now())],'0',1))
        await entered.wait()
        self.m._profile_revisions[self.user]=2
        await self.m._run_profile_update(self.user,'new',[Message(MsgRole.USER,'我喜欢详细解释',datetime.now())],'0',2)
        release.set();await old
        self.assertEqual((await self.m.get_profile(self.user))['response_style'],'detailed')
        self.assertEqual((await self.m.profile_status(self.user))['state'],'updated')
        await self.m.forget(self.user)

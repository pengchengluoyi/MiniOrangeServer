# !/usr/bin/env python
# -*-coding:utf-8 -*-

import asyncio



def mSleep(seconds):
    async def _sleep(seconds):
        await asyncio.sleep(seconds)  # 非阻塞等待

    asyncio.run(_sleep(seconds))


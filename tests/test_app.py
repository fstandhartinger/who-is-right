import asyncio
import app

def test_limits_reject_fourth_session():
    old=app.SESSIONS_PER_IP_DAY
    app.SESSIONS_PER_IP_DAY=3
    lim=app.Limits()
    try:
        async def run():
            for _ in range(3):
                assert await lim.enter("203.0.113.4") is None
                await lim.leave()
            assert "tomorrow" in (await lim.enter("203.0.113.4")).lower()
        asyncio.run(run())
    finally: app.SESSIONS_PER_IP_DAY=old

def test_global_jev_cap():
    old=app.GLOBAL_JEV_CALLS_DAY
    app.GLOBAL_JEV_CALLS_DAY=1
    lim=app.Limits()
    try:
        async def run():
            assert await lim.take_jev()
            assert not await lim.take_jev()
        asyncio.run(run())
    finally: app.GLOBAL_JEV_CALLS_DAY=old

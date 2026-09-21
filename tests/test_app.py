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

def test_chunked_transcript_becomes_one_complete_utterance():
    assembler=app.UtteranceAssembler()
    assembler.add(" The Eiffel Tower is",at=1.0)
    assembler.add(" in Ber",at=1.3)
    assembler.add("lin, I'm sure,",at=1.4)
    assembler.add(" it was buil",at=1.6)
    assembler.add("t in 1950",at=1.8)
    assert not assembler.ready(at=2.0)
    assert assembler.ready(at=3.3)
    utterance=assembler.flush()
    assert utterance == "The Eiffel Tower is in Berlin, I'm sure, it was built in 1950"
    assert app.claim_candidates(utterance) == ["The Eiffel Tower is in Berlin","The Eiffel Tower was built in 1950"]

def test_fragment_is_not_a_claim_candidate():
    assert app.claim_candidates('" equals 2"') == []

def test_debug_requires_exact_nonempty_token(monkeypatch):
    monkeypatch.setattr(app,"DEBUG_TOKEN","secret-token")
    assert app.debug_authorized("secret-token")
    assert not app.debug_authorized("")
    assert not app.debug_authorized("secret")

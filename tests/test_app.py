import asyncio
import app
from pathlib import Path

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
    assert not assembler.ready(at=3.3)
    assert assembler.ready(at=4.3)
    utterance=assembler.flush()
    assert utterance == "The Eiffel Tower is in Berlin, I'm sure, it was built in 1950"
    assert app.claim_candidates(utterance) == ["The Eiffel Tower is in Berlin","The Eiffel Tower was built in 1950"]

def test_fragment_is_not_a_claim_candidate():
    assert app.claim_candidates('" equals 2"') == []
    assert app.claim_candidates("are driving on the street.") == []

def test_recorded_session_pauses_do_not_emit_connectors_or_mid_thought_fragments():
    """Timing/chunks copied from Florian's 19:05 UTC debug recording."""
    assembler=app.UtteranceAssembler()
    for at,text in [(9.950," and"),(10.432," pla"),(10.592,"in"),(10.741,"s of"),
                    (11.106," ly"),(11.279,"ing"),(11.354," in"),(11.529," the"),
                    (11.712," air")]: assembler.add(text,at=at)
    assert not assembler.ready(at=13.112)  # old 1.35 s boundary fired here
    assembler.add(" and",at=13.824)
    assert not assembler.ready(at=15.225)  # old code emitted just "and"
    assembler.add(" a ca",at=15.745); assembler.add("t is",at=16.020)
    assembler.add(" an",at=16.666); assembler.add(" animal",at=16.825)
    assert not assembler.ready(at=18.125)
    assert assembler.ready(at=19.3)
    assert assembler.flush() == "and plains of lying in the air and a cat is an animal"

def test_debug_requires_exact_nonempty_token(monkeypatch):
    monkeypatch.setattr(app,"DEBUG_TOKEN","secret-token")
    assert app.debug_authorized("secret-token")
    assert not app.debug_authorized("")
    assert not app.debug_authorized("secret")

def test_live_model_and_blocking_check_claim_tool_are_truthful():
    assert app.LIVE_MODEL == "gemini-3.8-live"
    declaration=app.CHECK_CLAIM_TOOL["functionDeclarations"][0]
    assert declaration["name"] == "check_claim"
    assert declaration["behavior"] == "BLOCKING"
    assert declaration["parameters"]["required"] == ["claim","needs_web_check"]

def test_system_waits_for_complete_claims_and_ignores_opinions():
    instruction=app.LIVE_SYSTEM.lower()
    assert "complete statement across audio chunks" in instruction
    assert "opinions" in instruction and "feelings" in instruction

def test_pending_utterance_is_available_before_flush():
    assembler=app.UtteranceAssembler()
    assembler.add("Der FC Bayern ",at=1)
    assembler.add("hat heute gewonnen.",at=2)
    assert assembler.current() == "Der FC Bayern hat heute gewonnen."

def test_coin_flip_or_low_confidence_verdict_is_uncertain():
    assert app.calibrated_label("true",.5,.25) == "uncertain"
    assert app.calibrated_label("false",.2,.45) == "uncertain"
    assert app.calibrated_label("true",.9,.85) == "true"

def test_reaction_deck_uses_every_line_before_repeating_and_mentions_claim():
    deck=app.ReactionDeck(__import__("random").Random(7))
    count=len(app.REACTION_LINES["true"])
    lines=[deck.line("true","Canberra is the capital of Australia") for _ in range(count)]
    assert len(set(lines)) == count
    assert all("Canberra" in line for line in lines)
    assert deck.line("true","Canberra is the capital of Australia") in lines

def test_reaction_deck_covers_all_verdict_types():
    deck=app.ReactionDeck(__import__("random").Random(1))
    for kind in ("true","false","uncertain","not_a_fact"):
        assert "“Tea is warm”" in deck.line(kind,"Tea is warm")

def test_browser_explains_processing_and_connection_failures():
    js=(Path(__file__).parents[1]/"public"/"app.js").read_text()
    html=(Path(__file__).parents[1]/"public"/"index.html").read_text()
    assert "CHECKING…" in js
    assert "took too long to answer" in js
    assert "Couldn’t connect to the referee" in js
    assert html.count('aria-live="polite"') >= 2

def test_page_has_a_local_favicon():
    public=Path(__file__).parents[1]/"public"
    assert 'href="/favicon.svg"' in (public/"index.html").read_text()
    assert (public/"favicon.svg").read_text().startswith("<svg")

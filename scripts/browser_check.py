import asyncio, base64, json, sys
from pathlib import Path
from playwright.async_api import async_playwright

URL=sys.argv[1]
WAV=Path(sys.argv[2]).read_bytes() if len(sys.argv)>2 else b""
OUT=Path("artifacts"); OUT.mkdir(exist_ok=True)

async def main():
  async with async_playwright() as p:
    browser=await p.chromium.connect_over_cdp("http://127.0.0.1:9333")
    context=browser.contexts[0]
    page=await context.new_page()
    try:
      if WAV:
        encoded=base64.b64encode(WAV).decode()
        await page.add_init_script(f"""window.__sample='{encoded}';
        navigator.mediaDevices.getUserMedia=async()=>{{
          const c=new AudioContext(), d=c.createMediaStreamDestination();
          const b=Uint8Array.from(atob(window.__sample),x=>x.charCodeAt(0)).buffer;
          const a=await c.decodeAudioData(b), s=c.createBufferSource();
        s.buffer=a;s.connect(d);setTimeout(()=>s.start(),500);return d.stream;
        }};""")
      await page.set_viewport_size({"width":1280,"height":900})
      await page.goto(URL,wait_until="networkidle")
      assert "PARTY DEMO" in await page.locator("main").inner_text()
      await page.screenshot(path=str(OUT/"desktop.png"),full_page=True)
      if WAV:
        await page.get_by_role("button",name="START LISTENING").click()
        # A long utterance can legitimately show an intermediate NO CHECKABLE
        # CLAIM before a later complete clause gets its verdict. Let the whole
        # recording and backend pause window finish before asserting the final UI.
        await page.wait_for_timeout(18000)
        await page.wait_for_function("!document.querySelector('#score').textContent.includes('READY')",timeout=30000)
        print("LIVE_VERDICT",await page.locator("#score").inner_text())
        await page.screenshot(path=str(OUT/"live-verdict.png"),full_page=True)
      await page.set_viewport_size({"width":390,"height":844})
      await page.reload(wait_until="networkidle")
      await page.screenshot(path=str(OUT/"phone.png"),full_page=True)
      print("DESKTOP_PHONE_OK")
    finally:
      await page.close()
asyncio.run(main())

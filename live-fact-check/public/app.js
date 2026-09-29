const $ = (id) => document.getElementById(id);
const COLORS = { A: '#34d399', B: '#f87171', C: '#94a3b8', pending: '#818cf8', silence: '#333b49' };
const WORDS = { A: 'TRUE', B: 'FALSE', C: 'NOT A CLAIM' };
const CIRCUMFERENCE = 97.4;
const MAX_TICKS = 400;

let config = null;
let socket = null;
let context = null;
let stream = null;
let source = null;
let worklet = null;
let muted = null;
let mode = null;
let sampleText = '';
let startedAt = 0;
let timerHandle = 0;
let events = [];
let eventByTick = new Map();
let labels = [];
let latencies = [];
let resultTimes = [];
let stopPromise = null;
let resampleCarry = new Float32Array(0);
let resampleCursor = 0;

function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
function showStatus(text, active = false) {
  $('kicker').textContent = text;
  $('liveDot').classList.toggle('on', active);
}
function setVerdict(label, agreement, animate = true) {
  const card = $('verdict');
  const next = label || 'idle';
  const word = label ? WORDS[label] : 'READY';
  if ($('vword').textContent !== word && animate) {
    $('vword').classList.remove('pop');
    requestAnimationFrame(() => $('vword').classList.add('pop'));
  }
  card.dataset.v = next;
  $('vword').textContent = word;
  if (!label) {
    $('v-label').textContent = 'Live verdict';
    $('vclaim').textContent = 'Tap the mic or play a spoken sample. Each check hears a rolling 3-second window.';
  } else {
    const pct = agreement == null ? '' : ` · ${Math.round(agreement * 100)}% recent agreement`;
    $('v-label').textContent = `Live verdict · ${label}${pct}`;
    $('vclaim').textContent = sampleText ? `“${sampleText}”` : 'Latest complete statement in the rolling 3-second audio window.';
  }
}
function setBusy(active) {
  $('mic').classList.toggle('on', active && mode === 'mic');
  $('mic').setAttribute('aria-label', active && mode === 'mic' ? 'Stop microphone' : 'Start microphone');
  $('timer').classList.toggle('on', active);
  $('sampleBtn').disabled = active;
  document.querySelectorAll('.chip').forEach((button) => { button.disabled = active; });
  $('ctlTitle').textContent = active ? (mode === 'sample' ? 'Playing spoken sample' : 'Listening live') : 'Use your microphone';
  $('ctlSub').innerHTML = active ? 'Tap the mic to stop · each request is capped to the latest 3 seconds' : 'or <button class="link" id="sampleBtn">play a sample ▸</button> — no mic needed';
  const sampleButton = $('sampleBtn');
  if (sampleButton) sampleButton.addEventListener('click', playFirstSample, { once: true });
}
function resetRun() {
  events = [];
  eventByTick = new Map();
  labels = [];
  latencies = [];
  resultTimes = [];
  $('inflight').textContent = '0';
  $('last').textContent = '–';
  $('p50').textContent = '–';
  $('rps').textContent = '0.0';
  $('pat').textContent = $('pbt').textContent = $('pct').textContent = '–';
  $('pa').style.width = $('pb').style.width = $('pc').style.width = '0%';
  setVerdict(null, null, false);
  drawTimeline();
}

function setLabelMix() {
  const recent = labels.slice(-24);
  const count = { A: 0, B: 0, C: 0 };
  recent.forEach((label) => { if (count[label] != null) count[label] += 1; });
  const total = recent.length || 1;
  const pct = (key) => Math.round(count[key] * 100 / total);
  $('pa').style.width = `${pct('A')}%`;
  $('pb').style.width = `${pct('B')}%`;
  $('pc').style.width = `${pct('C')}%`;
  $('pat').textContent = `${pct('A')}%`;
  $('pbt').textContent = `${pct('B')}%`;
  $('pct').textContent = `${pct('C')}%`;
}

function eventFor(tick, t) {
  let event = eventByTick.get(tick);
  if (!event) {
    event = { tick, t: Number(t) || 0, status: 'pending', label: null, agreement: null };
    eventByTick.set(tick, event);
    events.push(event);
    if (events.length > MAX_TICKS) {
      const removed = events.shift();
      eventByTick.delete(removed.tick);
    }
  }
  return event;
}

function handleMessage(message) {
  if (message.type === 'ready') {
    $('modelName').textContent = message.model;
    $('timerTxt').textContent = `${message.seconds}s`;
    return;
  }
  if (message.type === 'sent') {
    eventFor(message.tick, message.t).status = 'pending';
    $('inflight').textContent = String(message.inflight ?? 0);
    drawTimeline();
    return;
  }
  if (message.type === 'result') {
    const event = eventFor(message.tick, message.t);
    event.status = 'result';
    event.label = message.label;
    event.agreement = message.agreement;
    labels.push(message.label);
    latencies.push(message.latency_ms);
    resultTimes.push(performance.now());
    resultTimes = resultTimes.filter((t) => performance.now() - t < 10000);
    $('last').textContent = String(message.latency_ms);
    const sorted = [...latencies].sort((a, b) => a - b);
    $('p50').textContent = String(sorted[Math.floor((sorted.length - 1) / 2)]);
    $('rps').textContent = (resultTimes.length / 10).toFixed(1);
    $('inflight').textContent = String(Math.max(0, Number($('inflight').textContent) - 1));
    setLabelMix();
    if (message.verdict?.label) setVerdict(message.verdict.label, message.verdict.agreement);
    drawTimeline();
    return;
  }
  if (message.type === 'tick') {
    const event = eventFor(message.tick, message.t);
    event.status = message.skipped || 'pending';
    if (message.verdict?.label) setVerdict(message.verdict.label, message.verdict.agreement);
    if (message.skipped === 'silence') $('inflight').textContent = '0';
    drawTimeline();
    return;
  }
  if (message.type === 'limited') {
    $('limitTitle').textContent = message.reason === 'busy' ? 'All listening slots are busy' : 'That’s enough for today';
    $('limitMsg').textContent = message.message;
    $('limit').hidden = false;
    stopSession();
    return;
  }
  if (message.type === 'error') {
    showStatus(message.message || 'The model is not answering right now.');
    return;
  }
  if (message.type === 'ended') {
    showStatus(message.message || 'Listening session ended.');
    stopSession();
  }
}

function websocketUrl() {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/ws`;
}
function connect() {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(websocketUrl());
    socket = ws;
    ws.binaryType = 'arraybuffer';
    const timeout = setTimeout(() => { ws.close(); reject(new Error('The live connection took too long.')); }, 12000);
    ws.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === 'ready') { clearTimeout(timeout); resolve(ws); }
      handleMessage(message);
    };
    ws.onerror = () => { clearTimeout(timeout); reject(new Error('Could not connect to the live model.')); };
    ws.onclose = (event) => {
      clearTimeout(timeout);
      if (event.code === 4429) {
        $('limitTitle').textContent = 'That’s enough for today';
        $('limitMsg').textContent = 'This address has reached its daily demo limit. Please try again tomorrow.';
        $('limit').hidden = false;
      }
      if (socket === ws && mode) finishSession();
    };
  });
}

function audioContext() {
  if (!context) {
    try { context = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' }); }
    catch { context = new AudioContext({ latencyHint: 'interactive' }); }
  }
  return context;
}
function floatToPcm16(input) {
  const ctx = audioContext();
  const sourceRate = ctx.sampleRate;
  let floats;
  if (sourceRate === 16000) {
    floats = input;
  } else {
    const merged = new Float32Array(resampleCarry.length + input.length);
    merged.set(resampleCarry, 0);
    merged.set(input, resampleCarry.length);
    const step = sourceRate / 16000;
    const out = [];
    while (resampleCursor + step <= merged.length) {
      const start = Math.floor(resampleCursor);
      const end = Math.max(start + 1, Math.floor(resampleCursor + step));
      let sum = 0;
      for (let i = start; i < end; i++) sum += merged[i] || 0;
      out.push(sum / (end - start));
      resampleCursor += step;
    }
    const consumed = Math.floor(resampleCursor);
    resampleCarry = merged.slice(consumed);
    resampleCursor -= consumed;
    floats = out;
  }
  const pcm = new Int16Array(floats.length);
  for (let i = 0; i < floats.length; i++) {
    const s = Math.max(-1, Math.min(1, floats[i]));
    pcm[i] = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767);
  }
  return pcm;
}
function sendFloats(input) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  const pcm = floatToPcm16(input);
  if (pcm.length) socket.send(pcm.buffer);
}

async function begin(modeName, text = '') {
  if (mode) return false;
  resetRun();
  mode = modeName;
  sampleText = text;
  resampleCarry = new Float32Array(0);
  resampleCursor = 0;
  try {
    const ctx = audioContext();
    await ctx.resume();
    const ws = await connect();
    if (socket !== ws) throw new Error('The live connection closed.');
    startedAt = performance.now();
    setBusy(true);
    showStatus(mode === 'sample' ? 'Sample audio is in the live pipeline.' : 'Listening · rolling 3-second audio · target 5 checks per second.', true);
    timerHandle = setInterval(updateTimer, 100);
    updateTimer();
    return true;
  } catch (error) {
    showStatus(error.message || 'Could not start the live session.');
    await finishSession();
    return false;
  }
}

function updateTimer() {
  if (!mode || !config) return;
  const elapsed = (performance.now() - startedAt) / 1000;
  const remaining = Math.max(0, config.sessionSeconds - elapsed);
  $('timerTxt').textContent = `${Math.ceil(remaining)}s`;
  $('timerProg').style.strokeDashoffset = String(CIRCUMFERENCE * elapsed / config.sessionSeconds);
  if (remaining <= 0) stopSession();
}

function attachMic() {
  const ctx = audioContext();
  return ctx.audioWorklet.addModule('/worklet.js').then(() => {
    source = ctx.createMediaStreamSource(stream);
    worklet = new AudioWorkletNode(ctx, 'capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    muted = ctx.createGain();
    muted.gain.value = 0;
    worklet.port.onmessage = (event) => sendFloats(event.data);
    source.connect(worklet);
    worklet.connect(muted);
    muted.connect(ctx.destination);
  });
}

async function startMic() {
  if (mode) { await stopSession(); return; }
  if (!navigator.mediaDevices?.getUserMedia) {
    showStatus('This browser does not allow microphone capture. Try the spoken samples below.');
    return;
  }
  let permission;
  try { permission = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
  catch { showStatus('Microphone permission is unavailable. Try a spoken sample.'); return; }
  if (!await begin('mic')) return;
  try {
    stream = await permission;
    await attachMic();
  } catch (error) {
    showStatus(error.name === 'NotAllowedError' ? 'Allow microphone access, or choose a spoken sample.' : 'Could not start the microphone.');
    await stopSession();
  }
}

async function playSample(sample) {
  if (!sample || mode) return;
  const response = await fetch(`/samples/${encodeURIComponent(sample.id)}.wav`, { cache: 'force-cache' });
  if (!response.ok) { showStatus('This audio sample could not be loaded.'); return; }
  const bytes = await response.arrayBuffer();
  const ctx = audioContext();
  let buffer;
  try { buffer = await ctx.decodeAudioData(bytes.slice(0)); }
  catch { showStatus('This browser could not decode the sample.'); return; }
  if (!await begin('sample', sample.text)) return;
  const channel = buffer.getChannelData(0);
  source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);
  source.start();
  document.querySelectorAll('.chip').forEach((b) => b.classList.toggle('playing', b.dataset.id === sample.id));
  const frame = Math.max(1, Math.floor(ctx.sampleRate * 0.18));
  for (let position = 0; position < channel.length && mode === 'sample'; position += frame) {
    sendFloats(channel.slice(position, Math.min(channel.length, position + frame)));
    await delay(180);
  }
  await delay(1800);
  document.querySelectorAll('.chip').forEach((b) => b.classList.remove('playing'));
  await stopSession();
}

function playFirstSample() {
  const first = config?.samples?.find((sample) => sample.label === 'A') || config?.samples?.[0];
  if (first) playSample(first).catch(() => showStatus('Could not play the audio sample.'));
}

async function stopSession() {
  if (stopPromise) return stopPromise;
  if (!mode) return;
  stopPromise = (async () => {
    if (mode === 'mic') {
      try { worklet?.port.close(); } catch {}
      try { source?.disconnect(); } catch {}
      try { worklet?.disconnect(); } catch {}
      try { muted?.disconnect(); } catch {}
      stream?.getTracks().forEach((track) => track.stop());
      stream = null;
    } else {
      try { source?.stop(); } catch {}
      try { source?.disconnect(); } catch {}
    }
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'stop' }));
      await delay(900);
      try { socket.close(1000, 'session finished'); } catch {}
    }
    await finishSession();
  })();
  await stopPromise;
  stopPromise = null;
}

async function finishSession() {
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = 0;
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  if (socket && socket.readyState < WebSocket.CLOSING) {
    try { socket.close(); } catch {}
  }
  socket = null;
  mode = null;
  source = null;
  worklet = null;
  muted = null;
  document.querySelectorAll('.chip').forEach((button) => button.classList.remove('playing'));
  setBusy(false);
  $('timerTxt').textContent = `${config?.sessionSeconds ?? 60}s`;
  $('timerProg').style.strokeDashoffset = '0';
  if (!$('limit').hidden) return;
  showStatus('Say a claim, question, opinion, or greeting. The model must choose A, B, or C.');
}

function drawTimeline() {
  const canvas = $('timeline');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const width = Math.max(1, Math.floor(rect.width * ratio));
  const height = Math.max(1, Math.floor(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = 'rgba(255,255,255,.055)';
  ctx.lineWidth = ratio;
  for (let i = 1; i < 4; i++) {
    const y = Math.round(height * i / 4);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
  const visible = events.slice(-MAX_TICKS);
  if (!visible.length) {
    ctx.fillStyle = '#687182';
    ctx.font = `${12 * ratio}px Inter, sans-serif`;
    ctx.fillText('Your verdicts will appear here as the audio moves through the 3-second window.', 20 * ratio, height / 2);
    return;
  }
  const gap = Math.max(1, ratio);
  const step = width / visible.length;
  const barWidth = Math.max(1.5 * ratio, step - gap);
  visible.forEach((event, index) => {
    const x = index * step + gap / 2;
    const label = event.label;
    if (label) {
      const strength = Math.min(1, Math.max(0.25, event.agreement || 0.45));
      const barHeight = height * (0.28 + strength * 0.48);
      ctx.globalAlpha = 0.50 + strength * 0.48;
      ctx.fillStyle = COLORS[label];
      ctx.fillRect(x, height - barHeight, barWidth, barHeight);
      ctx.globalAlpha = 1;
      if (barWidth > 12 * ratio) {
        ctx.fillStyle = '#07100f'; ctx.font = `600 ${9 * ratio}px Inter, sans-serif`;
        ctx.fillText(label, x + 3 * ratio, height - barHeight + 13 * ratio);
      }
    } else if (event.status === 'pending') {
      ctx.fillStyle = COLORS.pending;
      ctx.fillRect(x, height * 0.68, barWidth, height * 0.32);
    } else {
      ctx.fillStyle = COLORS.silence;
      ctx.fillRect(x, height * 0.87, barWidth, height * 0.13);
    }
  });
}

function renderSamples() {
  const host = $('chips');
  host.replaceChildren();
  for (const sample of config.samples) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chip';
    button.dataset.id = sample.id;
    button.textContent = `${sample.label} · ${WORDS[sample.label]} — ${sample.text}`;
    button.addEventListener('click', () => playSample(sample).catch(() => showStatus('Could not play this sample.')));
    host.append(button);
  }
}

async function initialize() {
  $('mic').addEventListener('click', startMic);
  $('sampleBtn').addEventListener('click', playFirstSample);
  $('limitClose').addEventListener('click', () => { $('limit').hidden = true; });
  window.addEventListener('resize', drawTimeline);
  try {
    const response = await fetch('/api/config', { cache: 'no-store' });
    if (!response.ok) throw new Error('config unavailable');
    config = await response.json();
    $('modelName').textContent = config.model;
    $('timerTxt').textContent = `${config.sessionSeconds}s`;
    renderSamples();
  } catch {
    showStatus('The live service is waking up. Reload in a moment.');
  }
  drawTimeline();
}

initialize();

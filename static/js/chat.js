/**
 * voice.js — push-to-talk, Whisper transcription, LLM + TTS pipeline.
 *
 * States: INIT → GREETING → IDLE → RECORDING → TRANSCRIBING → PREVIEW → PROCESSING → PLAYING → IDLE
 */

let state           = "INIT";
let mediaStream     = null;
let recorder        = null;
let audioChunks     = [];
let currentAudio    = null;
let currentTurnNum  = 0;   // updated after each server response
let storyRoundsDone = 0;   // story turns completed (turn_number - INTRO_TURNS)
// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  requestMicPermission().then(() => {
    document.getElementById("countdownGate").style.display = "flex";
    startCountdown();
  });
});

async function requestMicPermission() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setStatus("error", "Microphone access denied — please allow microphone and refresh.");
    throw e;
  }
}

// ── Countdown gate ────────────────────────────────────────────────────────────
function startCountdown() {
  let remaining = 20;
  const numEl   = document.getElementById("countdownNum");
  const readyBtn = document.getElementById("readyBtn");

  const tick = setInterval(() => {
    remaining -= 1;
    if (numEl) numEl.textContent = remaining;
    if (remaining <= 0) {
      clearInterval(tick);
      if (numEl) numEl.style.display = "none";
      if (readyBtn) readyBtn.style.display = "";
    }
  }, 1000);
}

function onReady() {
  const gate = document.getElementById("countdownGate");
  if (gate) gate.style.display = "none";
  document.getElementById("voiceInputArea").style.display = "";
  loadGreeting();
}

async function loadGreeting() {
  setStatus("loading", "Loading…");
  try {
    const res  = await fetch("/api/greeting");
    const data = await res.json();
    if (!data.ok) throw new Error("Session error");

    appendMessage("ai", data.opening_text);
    await playAudio(data.tts_b64);
  } catch (e) {
    setStatus("error", "Could not load session. Please refresh.");
  }
}

// ── Recording ─────────────────────────────────────────────────────────────────
async function toggleRecording() {
  if (state === "IDLE") {
    await startRecording();
  } else if (state === "RECORDING") {
    stopRecording();
  }
}

async function startRecording() {
  if (state !== "IDLE") return;
  setState("RECORDING");

  audioChunks = [];

  if (!mediaStream || mediaStream.getTracks().every(t => t.readyState === "ended")) {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  }

  recorder = new MediaRecorder(mediaStream, { mimeType: bestMimeType() });
  recorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
  recorder.start(100);
}

function stopRecording() {
  if (state !== "RECORDING" || !recorder) return;

  recorder.onstop = async () => {
    const blob = new Blob(audioChunks, { type: recorder.mimeType });
    await transcribeAudio(blob);
  };
  recorder.stop();
  setState("TRANSCRIBING");
}

function bestMimeType() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  return candidates.find(t => MediaRecorder.isTypeSupported(t)) || "";
}

// ── Transcription ─────────────────────────────────────────────────────────────
async function transcribeAudio(blob) {
  setStatus("processing", "Transcribing…");
  try {
    const fd = new FormData();
    fd.append("audio", blob, "audio.webm");
    const res  = await fetch("/api/transcribe", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error("Transcription failed");

    showPreview(data.transcript);
    setState("PREVIEW");
  } catch (err) {
    setStatus("error", "Transcription error — please try again.");
    setState("IDLE");
  }
}

// ── Re-record ─────────────────────────────────────────────────────────────────
function reRecord() {
  showPTT();
  setState("IDLE");
}

// ── Submit turn ───────────────────────────────────────────────────────────────
async function submitTurn() {
  if (state !== "PREVIEW") return;
  setState("PROCESSING");

  const transcript  = document.getElementById("transcriptText").value.trim();
  const thisTurn    = currentTurnNum + 1;
  const storyRound  = thisTurn > 2 ? thisTurn - 2 : null;
  appendMessage("user", transcript, storyRound);
  hideInputArea();
  showTypingIndicator();

  // Update counter immediately when user responds (not after AI reply)
  storyRoundsDone = Math.max(0, thisTurn - 1);
  updateRoundDisplay();

  try {
    const res  = await fetch("/api/turn", { method: "POST" });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Server error");

    removeTypingIndicator();
    appendMessage("ai", data.ai_text, null);
    currentTurnNum  = data.turn_number;
    await playAudio(data.tts_b64, data.is_final ? showCompletionModal : null);

    if (data.is_final) return;
  } catch (err) {
    removeTypingIndicator();
    setStatus("error", `Error: ${err.message}. Please try again.`);
    setState("IDLE");
    showPTT();
  }
}

// ── Audio playback ────────────────────────────────────────────────────────────
function playAudio(b64mp3, onEnded) {
  return new Promise((resolve) => {
    setState("PLAYING");
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }

    const blob = b64ToBlob(b64mp3, "audio/mpeg");
    const url  = URL.createObjectURL(blob);
    currentAudio = new Audio(url);

    currentAudio.onended = () => {
      URL.revokeObjectURL(url);
      currentAudio = null;
      setState("IDLE");
      showPTT();
      resolve();
      if (onEnded) onEnded();
    };
    currentAudio.onerror = () => {
      URL.revokeObjectURL(url);
      setState("IDLE");
      showPTT();
      resolve();
    };
    currentAudio.play().catch(() => {
      setState("IDLE");
      showPTT();
      resolve();
    });
  });
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function showPTT() {
  document.getElementById("pttState").style.display = "";
  document.getElementById("previewState").style.display = "none";
  document.getElementById("voiceInputArea").style.display = "";
}

function showPreview(transcript) {
  document.getElementById("transcriptText").value = transcript;
  document.getElementById("pttState").style.display = "none";
  document.getElementById("previewState").style.display = "";
  document.getElementById("voiceInputArea").style.display = "";
  setStatus("idle", "Review your message, then send");
}

function hideInputArea() {
  const area = document.getElementById("voiceInputArea");
  if (area) area.style.display = "none";
}


function showTypingIndicator() {
  const el = document.createElement("div");
  el.id = "typingIndicator";
  el.className = "message ai-message";
  el.innerHTML = `<div class="msg-bubble typing-indicator"><span></span><span></span><span></span></div>`;
  document.getElementById("chatMessages").appendChild(el);
  el.scrollIntoView({ behavior: "smooth", block: "end" });
}

function removeTypingIndicator() {
  const el = document.getElementById("typingIndicator");
  if (el) el.remove();
}

// ── State machine ─────────────────────────────────────────────────────────────
function setState(newState) {
  state = newState;
  const pttBtn = document.getElementById("pttBtn");

  const labelEl = document.getElementById("pttLabel");

  switch (newState) {
    case "IDLE":
      setStatus("idle", "Click the button to speak");
      if (pttBtn) { pttBtn.classList.remove("recording"); pttBtn.disabled = false; }
      if (labelEl) labelEl.textContent = "Click to speak";
      break;
    case "RECORDING":
      setStatus("recording", "Recording…");
      if (pttBtn) pttBtn.classList.add("recording");
      if (labelEl) labelEl.textContent = "Click to stop";
      break;
    case "TRANSCRIBING":
      setStatus("processing", "Transcribing…");
      if (pttBtn) { pttBtn.classList.remove("recording"); pttBtn.disabled = true; }
      if (labelEl) labelEl.textContent = "Processing…";
      break;
    case "PREVIEW":
      // status set in showPreview()
      break;
    case "PROCESSING":
      setStatus("processing", "Sage is thinking…");
      break;
    case "PLAYING":
      setStatus("playing", "Sage is speaking…");
      break;
    case "GREETING":
      setStatus("loading", "Loading…");
      break;
  }
}

function setStatus(type, text) {
  const dot  = document.getElementById("statusDot");
  const txtEl = document.getElementById("statusText");
  if (txtEl) txtEl.textContent = text;
  if (dot) { dot.className = `sage-status-dot ${type}`; }
}

// ── Message rendering ─────────────────────────────────────────────────────────
function appendMessage(role, text, roundNum) {
  const container = document.getElementById("chatMessages");

  if (role === "user") {
    const pair = document.createElement("div");
    pair.className = "message-pair";
    if (roundNum) pair.id = "pair-" + roundNum;

    const msg = document.createElement("div");
    msg.className = "message user-message msg-new";
    const metaText = roundNum ? `You \u2022 moment ${roundNum}` : "You";
    msg.innerHTML =
      `<div class="msg-bubble">${escapeHtml(text)}</div>` +
      `<div class="msg-meta">${metaText}</div>`;
    pair.appendChild(msg);
    container.appendChild(pair);
    pair.scrollIntoView({ behavior: "smooth", block: "end" });
  } else {
    const pairs    = container.querySelectorAll(".message-pair");
    let lastPair   = pairs[pairs.length - 1];
    if (!lastPair) {
      lastPair = document.createElement("div");
      lastPair.className = "message-pair";
      container.appendChild(lastPair);
    }
    const msg = document.createElement("div");
    msg.className = "message ai-message msg-new";
    msg.innerHTML =
      `<div class="msg-bubble">${escapeHtml(text)}</div>` +
      `<div class="msg-meta">Sage</div>`;
    lastPair.appendChild(msg);
    msg.scrollIntoView({ behavior: "smooth", block: "end" });
  }
}

function updateRoundDisplay() {
  const el = document.getElementById("currentRound");
  if (el) el.textContent = storyRoundsDone;
  document.querySelectorAll(".sage-dot").forEach(function (dot, i) {
    dot.classList.toggle("sage-dot-done", i < storyRoundsDone);
  });
}

function showCompletionModal() {
  hideInputArea();
  setStatus("idle", "Conversation complete");
  const modal = document.getElementById("redirectModal");
  if (modal) modal.style.display = "flex";
}

function escapeHtml(text) {
  return text
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;")
    .replace(/'/g,  "&#039;");
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function b64ToBlob(b64, mime) {
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return new Blob([buf], { type: mime });
}

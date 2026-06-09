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
    console.log(`[recording] chunks=${audioChunks.length}, blob size=${blob.size}B`);

    // < 8KB ≈ <1 second of speech (or pure silence). Likely a mic problem.
    if (blob.size < 8000) {
      setState("IDLE");
      showPTT();
      // call last so the error message isn't overwritten by IDLE's default text
      setStatus("error", "Recording seems empty — please check your mic and try again.");
      return;
    }

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
    if (!data.ok) {
      // Backend reported a friendly reason (e.g. Whisper got empty text)
      setState("IDLE");
      showPTT();
      setStatus("error", data.error || "Transcription failed — please try again.");
      return;
    }
    showPreview(data.transcript);
    setState("PREVIEW");
  } catch (err) {
    setState("IDLE");
    showPTT();
    setStatus("error", "Transcription error — please try again.");
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

  const transcript = document.getElementById("transcriptText").value.trim();
  const thisTurn   = currentTurnNum + 1;
  appendMessage("user", transcript, labelForTurn(thisTurn));
  hideInputArea();
  showTypingIndicator();

  // Mark the user's just-submitted turn as done in the segmented progress
  updateProgress(thisTurn);

  try {
    const fd = new FormData();
    fd.append("transcript", transcript);   // send the (possibly edited) text
    const res  = await fetch("/api/turn", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Server error");

    removeTypingIndicator();
    appendMessage("ai", data.ai_text, null);
    currentTurnNum = data.turn_number;

    // After the closing turn of this topic, audio plays, then we redirect to post-survey
    const afterAudio = data.is_final ? showCompletionModal : null;

    await playAudio(data.tts_b64, afterAudio);
    if (data.is_final) return;
  } catch (err) {
    removeTypingIndicator();
    setState("IDLE");
    showPTT();
    // call setStatus LAST so the error message isn't overwritten by IDLE's default text
    setStatus("error", `Error: ${err.message}. Please try again.`);
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
      if (onEnded) {
        // onEnded handler (transition / completion) is responsible for what comes next
        onEnded();
      } else {
        setState("IDLE");
        showPTT();
      }
      resolve();
    };
    currentAudio.onerror = () => {
      URL.revokeObjectURL(url);
      if (!onEnded) {
        setState("IDLE");
        showPTT();
      }
      resolve();
    };
    currentAudio.play().catch(() => {
      if (!onEnded) {
        setState("IDLE");
        showPTT();
      }
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
      setStatus("processing", `${AI_NAME} is thinking…`);
      break;
    case "PLAYING":
      setStatus("playing", `${AI_NAME} is speaking…`);
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
    const metaText = roundNum ? `You \u2022 ${roundNum}` : "You";
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
      `<div class="msg-meta">${AI_NAME}</div>`;
    lastPair.appendChild(msg);
    msg.scrollIntoView({ behavior: "smooth", block: "end" });
  }
}

// ── Progress (X of 5 moments for the current topic) ──────────────────────────
function updateProgress(justCompletedTurn) {
  // Intro (turn 1) doesn't count as a "moment". Each topic has PER_TOPIC_TURNS moments.
  let turnInTopic = 0;
  if (justCompletedTurn >= 2) {
    turnInTopic = ((justCompletedTurn - 2) % PER_TOPIC_TURNS) + 1;
  }

  const numEl = document.getElementById("currentRound");
  if (numEl) numEl.textContent = turnInTopic;

  document.querySelectorAll(".sage-dot").forEach((dot, i) => {
    dot.classList.toggle("sage-dot-done", i < turnInTopic);
  });
}

function labelForTurn(turnNumber) {
  if (turnNumber < 1) return null;
  return `Turn ${turnNumber}`;
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

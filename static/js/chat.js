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
let chatDone        = false; // true once the topic's chat is complete (suppresses leave-warnings)

// Behavioral timing — raw ISO timestamps. Reset per turn cycle; sent with /api/turn.
// `ai_audio_ended_at` carries over from the previous AI response's playback end
// (set in playAudio.onended). The other four are set during the current user turn.
let turnTimings = {
  ai_audio_ended_at: null,
  record_started_at: null,
  record_ended_at:   null,
  preview_shown_at:  null,
  submitted_at:      null,
};

// Scratchpad drafting-behavior tracking — reset after each turn is submitted.
// `draftLastLength` is the only running state we need to detect a "got shorter"
// edit (a deletion); we don't keep intermediate drafts, just this turn's final snapshot.
let draftLastLength    = 0;
let draftStartedAt     = null;
let draftRevisionCount = 0;

// Recording timer — updates the button label with a live "00:45" style
// elapsed-time readout while state === "RECORDING". Same look throughout,
// no thresholds that change color/size.
let recordingTimerHandle = null;
let recordingStartMs     = null;
let pttTipDefaultHTML    = null;

function formatMMSS(totalSeconds) {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function updateRecordingLabel() {
  const labelEl = document.getElementById("pttLabel");
  if (!labelEl) return;
  const elapsed = Math.floor((Date.now() - recordingStartMs) / 1000);
  labelEl.textContent = `🟠 ${formatMMSS(elapsed)} Click to stop`;
}

function startRecordingTimer() {
  recordingStartMs = Date.now();
  updateRecordingLabel();
  recordingTimerHandle = setInterval(updateRecordingLabel, 500);
}

function stopRecordingTimer() {
  if (recordingTimerHandle) { clearInterval(recordingTimerHandle); recordingTimerHandle = null; }
}

function nowISO() { return new Date().toISOString(); }
// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  // Cache the default (non-recording) tip text so we can restore it after
  // swapping in the "Recording..." message.
  const tipTextEl = document.getElementById("pttTipText");
  if (tipTextEl) pttTipDefaultHTML = tipTextEl.innerHTML;

  // Auto-grow the transcript review textarea as the participant edits it.
  const transcriptEl = document.getElementById("transcriptText");
  if (transcriptEl) transcriptEl.addEventListener("input", () => autoGrow(transcriptEl));

  // Auto-grow the private notes scratchpad too (capped by max-height in CSS),
  // and track drafting behavior: first-keystroke timestamp + "got shorter" count.
  const scratchpadEl = document.getElementById("scratchpad");
  if (scratchpadEl) scratchpadEl.addEventListener("input", () => {
    autoGrow(scratchpadEl);
    const len = scratchpadEl.value.length;
    if (draftStartedAt === null && len > 0) draftStartedAt = nowISO();
    if (len < draftLastLength) draftRevisionCount++;
    draftLastLength = len;
  });

  requestMicPermission().then(() => {
    if (typeof HAS_EXISTING_CHAT !== "undefined" && HAS_EXISTING_CHAT) {
      // Returning to a chat already in progress — skip countdown, rehydrate from DB.
      document.getElementById("countdownGate").style.display = "none";
      document.getElementById("voiceInputArea").style.display = "";
      resumeChat();
    } else {
      document.getElementById("countdownGate").style.display = "flex";
      startCountdown();
    }
  });
});

// Warn the user before they accidentally navigate away mid-conversation.
// `beforeunload` catches refresh / tab-close.
window.addEventListener("beforeunload", (e) => {
  if (chatDone) return;   // conversation is done — let them navigate freely
  const inConversation = ["IDLE", "RECORDING", "TRANSCRIBING", "PREVIEW", "PROCESSING", "PLAYING"].includes(state);
  if (inConversation && currentTurnNum > 0) {
    e.preventDefault();
    e.returnValue = "You're in the middle of a conversation — leave?";
    return e.returnValue;
  }
});

// `beforeunload` does NOT fire for browser back / forward / trackpad swipe.
// Push a duplicate history entry on load, then intercept `popstate` to confirm.
history.pushState({stayPut: true}, "", location.href);
window.addEventListener("popstate", () => {
  if (chatDone) { history.back(); return; }   // conversation done — let them navigate
  const inConversation = ["IDLE", "RECORDING", "TRANSCRIBING", "PREVIEW", "PROCESSING", "PLAYING"].includes(state);
  if (inConversation && currentTurnNum > 0) {
    if (confirm("You're in the middle of a conversation — leave? Your progress is saved, but please don't navigate away.")) {
      history.back();   // user really wants to leave — let them
    } else {
      // Re-arm the trap by pushing state again
      history.pushState({stayPut: true}, "", location.href);
    }
  } else {
    history.back();
  }
});

// Debug: confirm the resume flag the server sent
console.log("[chat] HAS_EXISTING_CHAT =", typeof HAS_EXISTING_CHAT !== "undefined" ? HAS_EXISTING_CHAT : "undefined");

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

async function resumeChat() {
  setStatus("loading", "Resuming your conversation…");
  try {
    const res  = await fetch("/api/greeting");
    const data = await res.json();
    if (!data.ok) throw new Error("Session error");

    // Show the original greeting text (silent — no audio replay on resume)
    appendMessage("ai", data.opening_text);

    // Replay every saved turn into the chat window
    for (const msg of (data.existing_turns || [])) {
      if (msg.role === "user") {
        appendMessage("user", msg.text, labelForTurn(msg.turn));
      } else {
        appendMessage("ai", msg.text, null);
      }
    }

    currentTurnNum = data.current_turn_number || 0;
    updateProgress(currentTurnNum);

    setState("IDLE");
    showPTT();
  } catch (e) {
    setStatus("error", "Could not resume session. Please refresh.");
  }
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
  // Stamp the moment the participant chose to start speaking — end anchor for
  // hesitation, start anchor for speaking duration.
  turnTimings.record_started_at = nowISO();
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
  // Stamp the click — end of speaking duration.
  turnTimings.record_ended_at = nowISO();

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
  // Stamp the click — end of editing duration.
  turnTimings.submitted_at = nowISO();
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
    // Send all 5 raw behavioral timestamps for this turn (any may be null,
    // e.g. ai_audio_ended_at on the very first turn after a fresh page load).
    if (turnTimings.ai_audio_ended_at) fd.append("ai_audio_ended_at", turnTimings.ai_audio_ended_at);
    if (turnTimings.record_started_at) fd.append("record_started_at", turnTimings.record_started_at);
    if (turnTimings.record_ended_at)   fd.append("record_ended_at",   turnTimings.record_ended_at);
    if (turnTimings.preview_shown_at)  fd.append("preview_shown_at",  turnTimings.preview_shown_at);
    if (turnTimings.submitted_at)      fd.append("submitted_at",      turnTimings.submitted_at);

    // Scratchpad snapshot for this turn — whatever's in the box right now,
    // plus this turn's drafting-behavior signals.
    const draftText = document.getElementById("scratchpad").value;
    fd.append("draft_final_text", draftText);
    fd.append("draft_char_count", String(draftText.length));
    fd.append("draft_revision_count", String(draftRevisionCount));
    if (draftStartedAt) fd.append("draft_started_at", draftStartedAt);

    const res  = await fetch("/api/turn", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Server error");

    // Reset turn timings for the next cycle. `ai_audio_ended_at` will get
    // re-stamped when the AI's response audio (about to play) finishes.
    turnTimings = {
      ai_audio_ended_at: null, record_started_at: null,
      record_ended_at:   null, preview_shown_at:  null, submitted_at: null,
    };
    // Reset drafting-behavior tracking for the next turn (the scratchpad text
    // itself is left untouched — it persists across turns within a topic).
    draftLastLength    = draftText.length;
    draftStartedAt     = null;
    draftRevisionCount = 0;

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
      // Stamp the exact moment AI audio finished — this is the start anchor for
      // the participant's hesitation on their NEXT turn.
      turnTimings.ai_audio_ended_at = nowISO();

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

// ── Collapsible topic box / sidebar ────────────────────────────────────────────
function toggleTopicBox() {
  const box = document.getElementById("topicBox");
  const btn = document.getElementById("topicCollapseBtn");
  const collapsed = box.classList.toggle("collapsed");
  btn.textContent = collapsed ? "Show" : "Hide";
}

function toggleSidebar() {
  const layout = document.getElementById("chatLayout");
  const btn    = document.getElementById("sidebarToggleBtn");
  const collapsed = layout.classList.toggle("sidebar-collapsed");
  btn.innerHTML = collapsed ? "&#9654; Show info" : "&#9664; Hide info";
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function showPTT() {
  document.getElementById("pttState").style.display = "";
  document.getElementById("previewState").style.display = "none";
  document.getElementById("voiceInputArea").style.display = "";
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}

function showPreview(transcript) {
  // Stamp the moment the editable preview becomes visible — start of editing duration.
  turnTimings.preview_shown_at = nowISO();
  const textEl = document.getElementById("transcriptText");
  textEl.value = transcript;
  document.getElementById("pttState").style.display = "none";
  document.getElementById("previewState").style.display = "";
  document.getElementById("voiceInputArea").style.display = "";
  setStatus("idle", "Review your message, then send");
  autoGrow(textEl);
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

  const labelEl  = document.getElementById("pttLabel");
  const tipTextEl = document.getElementById("pttTipText");

  switch (newState) {
    case "IDLE":
      setStatus("idle", "Click the button to speak");
      if (pttBtn) { pttBtn.classList.remove("recording"); pttBtn.disabled = false; }
      if (labelEl) labelEl.textContent = "Click to speak";
      stopRecordingTimer();
      if (tipTextEl && pttTipDefaultHTML !== null) tipTextEl.innerHTML = pttTipDefaultHTML;
      break;
    case "RECORDING":
      setStatus("recording", "Recording…");
      if (pttBtn) pttBtn.classList.add("recording");
      startRecordingTimer();
      if (tipTextEl) tipTextEl.textContent = "🟠 Recording... speak naturally, click again when you're done.";
      break;
    case "TRANSCRIBING":
      setStatus("processing", "Transcribing…");
      if (pttBtn) { pttBtn.classList.remove("recording"); pttBtn.disabled = true; }
      if (labelEl) labelEl.textContent = "Processing…";
      stopRecordingTimer();
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
  chatDone = true;   // suppress leave-warnings — navigating to /post-survey is expected
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

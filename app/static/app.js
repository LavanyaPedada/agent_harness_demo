// Analytics Agent — chat UI + live SSE event stream

const els = {
  chatLog: document.getElementById("chat-log"),
  chatForm: document.getElementById("chat-form"),
  chatInput: document.getElementById("chat-input"),
  chatSend: document.getElementById("chat-send"),
  traceLog: document.getElementById("trace-log"),
  wfLabel: document.getElementById("wf-label"),
  badge: document.getElementById("status-badge"),
  temporalLink: document.getElementById("temporal-link"),
  hTemporal: document.getElementById("health-temporal"),
  hOllama: document.getElementById("health-ollama"),
  hMemory: document.getElementById("health-memory"),
  hSkills: document.getElementById("health-skills"),
  reloadSkillsBtn: document.getElementById("reload-skills-btn"),
  hitlToggle: document.getElementById("hitl-toggle"),
  forceFailToggle: document.getElementById("force-fail-toggle"),
  approvalBanner: document.getElementById("approval-banner"),
  approvalSkill: document.getElementById("approval-skill"),
  approveBtn: document.getElementById("approve-btn"),
  denyBtn: document.getElementById("deny-btn"),
  ctrAttempts: document.getElementById("ctr-attempts"),
  ctrHttp: document.getElementById("ctr-http"),
  ctrTokens: document.getElementById("ctr-tokens"),
  skills: Array.from(document.querySelectorAll(".skill")),
  resetBtn: document.getElementById("reset-mem-btn"),
  planCard: document.getElementById("plan-card"),
  planSteps: document.getElementById("plan-steps"),
  planSource: document.getElementById("plan-source"),
  traceToggle: document.getElementById("trace-toggle"),
  workerStopBtn: document.getElementById("worker-stop-btn"),
  workerStartBtn: document.getElementById("worker-start-btn"),
  autoStopBtn: document.getElementById("auto-stop-btn"),
  usageToggle: document.getElementById("usage-toggle"),
  usagePanel: document.getElementById("usage-panel"),
  usageClose: document.getElementById("usage-close"),
  uProvider: document.getElementById("usage-provider"),
  uCalls: document.getElementById("u-calls"),
  uPrompt: document.getElementById("u-prompt"),
  uCompletion: document.getElementById("u-completion"),
  uTotal: document.getElementById("u-total"),
  uLatency: document.getElementById("u-latency"),
  uAvg: document.getElementById("u-avg"),
  uRecent: document.getElementById("u-recent"),
};

const state = {
  workflowId: null,
  eventSource: null,
  pendingMsgEl: null,
  attempts: 0,
  http: 0,
  pollTimer: null,
  taskQueueTimer: null,
  workflowDone: false,
  workerWasAlive: null,   // tri-state: null=unknown, true=alive, false=disconnected
  planSkillStarts: {},    // skill -> start_ts (for elapsed timing)
  autoStopArmed: false,   // when true, server kills worker at weather_fetch.start
  pendingApproval: null,  // skill name currently waiting on user approval
  hitlEnabled: false,
  forceFail: false,
};

// ---------- rendering ----------
function fmtTs(tsSec) {
  const d = new Date(tsSec * 1000);
  return d.toTimeString().slice(0, 8);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function setBadge(text, cls) {
  els.badge.textContent = text;
  els.badge.className = "badge " + cls;
}

function appendChat(role, body, opts = {}) {
  const div = document.createElement("div");
  div.className = "msg msg-" + (opts.cls || role);
  div.innerHTML = `<div class="msg-role">${escapeHtml(role)}</div><div class="msg-body${opts.thinking ? " thinking" : ""}">${escapeHtml(body)}</div>`;
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  return div.querySelector(".msg-body");
}

function appendTrace(evt) {
  const ts = fmtTs(evt.ts);
  const kindCls = "kind-" + evt.kind.replace(/\./g, "-");
  const row = document.createElement("div");
  row.className = "trace-row " + kindCls;

  const srcTag = evt.src ? `<span class="src">${escapeHtml(evt.src)}</span>` : "";
  row.innerHTML = `
    <span class="trace-ts">${ts}</span>
    <span class="trace-kind">${escapeHtml(evt.kind)}</span>
    <span class="trace-msg">${escapeHtml(evt.msg || "")}${srcTag}</span>
  `;

  // Detail block for failures, codegen, planner.emit, http.request, context.compact, memory.write
  const detail = renderDetail(evt);
  if (detail) row.appendChild(detail);

  els.traceLog.appendChild(row);
  els.traceLog.scrollTop = els.traceLog.scrollHeight;
}

function renderDetail(evt) {
  const p = evt.payload || {};
  const make = (text, cls = "") => {
    const d = document.createElement("div");
    d.className = "trace-detail" + (cls ? " " + cls : "");
    d.textContent = text;
    return d;
  };
  switch (evt.kind) {
    case "planner.emit":
      if (p.plan && p.plan.length) {
        const lines = p.plan.map((s) => `${s.step}. ${s.skill} — ${s.reason || ""}`).join("\n");
        return make(lines);
      }
      break;
    case "agent.codegen":
      if (p.code) return make(p.code);
      break;
    case "sandbox.fail":
      if (p.stderr) return make(p.stderr.trim(), "fail");
      break;
    case "sandbox.ok":
      return make(`exit=${p.exit_code}  has_artifact=${p.has_artifact}`, "ok");
    case "http.request":
      return make(`${p.url}\nparams: ${JSON.stringify(p.params)}\nstatus: ${p.status} (${p.elapsed_ms}ms)`);
    case "context.compact":
      return make(`Before: ${p.before_tokens} tokens   After: ${p.after_tokens} tokens   Saved: ${p.saved_tokens}\nSummary:\n${p.summary || ""}`);
    case "memory.write":
      if (p.entry) {
        return make(`pattern: ${p.entry.pattern}\nfix:     ${p.entry.fix}\ncount:   ${p.entry.count}\nfile:    ${p.agent_md_path}`, "ok");
      }
      break;
    case "memory.read":
      if (p.patterns && p.patterns.length) {
        return make("loaded:\n  - " + p.patterns.join("\n  - "));
      }
      return make("(no prior lessons)");
  }
  return null;
}

// ---------- event handling ----------
function handleEvent(evt) {
  if (evt.kind === "heartbeat") return;
  appendTrace(evt);

  // Update UI side-effects
  if (evt.kind === "planner.emit" || evt.kind === "planner.cached") {
    renderPlan(evt);
  } else if (evt.kind === "skill.start") {
    setSkill(evt.msg, "active");
    markPlanStep(evt.msg, "running", evt);
    // Auto-stop fires on weather_fetch.start — UI flips badge instantly and
    // POSTs /api/worker/stop directly so we don't wait for any server-side
    // watcher. Disarm after firing.
    if (state.autoStopArmed && evt.msg === "weather_fetch") {
      state.autoStopArmed = false;
      els.autoStopBtn.classList.remove("armed");
      els.autoStopBtn.textContent = "⚙ Auto-stop @ weather_fetch";
      state.userStoppedWorker = true;
      state.workerWasAlive = false;
      setBadge("worker disconnected", "badge-frozen");
      els.workerStopBtn.disabled = true;
      els.workerStartBtn.disabled = false;
      fetch("/api/worker/stop", { method: "POST" }).catch(() => {});
    }
  } else if (evt.kind === "skill.end") {
    setSkill(evt.msg, "done");
    markPlanStep(evt.msg, "done", evt);
  } else if (evt.kind === "agent.codegen") {
    state.attempts = Math.max(state.attempts, evt.payload?.attempt || 0);
    els.ctrAttempts.textContent = state.attempts;
  } else if (evt.kind === "http.request") {
    state.http += 1;
    els.ctrHttp.textContent = state.http;
  } else if (evt.kind === "context.compact") {
    const p = evt.payload || {};
    els.ctrTokens.textContent = `${p.before_tokens}→${p.after_tokens}`;
    appendCard(
      "🗜️ Context compacted",
      `${p.before_tokens} → ${p.after_tokens} tokens (saved ${p.saved_tokens}). ${p.summarised_entries} older turn(s) merged into a summary.`,
      "card-compact",
    );
  } else if (evt.kind === "memory.write") {
    refreshMemoryCount();
    const p = evt.payload || {};
    if (p.entry) {
      appendCard(
        p.updated ? "📚 Memory reinforced" : "📚 Memory updated",
        `Pattern: ${p.entry.pattern}\nFix: ${p.entry.fix}\nFile: ${p.agent_md_path}`,
        "card-memory",
      );
    }
  } else if (evt.kind === "planner.replan" || evt.kind === "planner.remedy") {
    const p = evt.payload || {};
    appendCard(
      "🔁 Replan triggered",
      `Failed step: ${p.failed_skill || "?"}\nReason: ${(p.failure_message || p.failure_kind || "").slice(0, 200)}\nNew plan: ${(p.plan || []).map(s => s.skill).join(" → ") || "(pending)"}`,
      "card-replan",
    );
    if (p.plan && p.plan.length) {
      drawPlan(p.plan);
      els.planSource.textContent = `replanned · ${p.plan.length} step(s)`;
    }
  } else if (evt.kind === "tool.invalid_args") {
    const p = evt.payload || {};
    appendCard(
      "❌ Tool failure",
      `${p.skill || "?"}: missing ${(p.missing || []).join(", ") || "(see message)"}\nWorkflow will replan to fix this.`,
      "card-fail",
    );
  } else if (evt.kind === "skills.registered") {
    refreshSkillsCount();
  }
}

function appendCard(title, body, cls) {
  const div = document.createElement("div");
  div.className = "harness-card " + (cls || "");
  div.innerHTML = `<div class="hc-title">${escapeHtml(title)}</div><pre class="hc-body">${escapeHtml(body)}</pre>`;
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

function renderProvenance(p) {
  const perKey = p.per_key || {};
  const keys = Object.keys(perKey);
  if (!keys.length) return;
  const div = document.createElement("div");
  div.className = "harness-card card-provenance";
  const lines = keys.map((k) => {
    const v = perKey[k] || {};
    const summary = v.summary ? `${v.summary.type}${v.summary.len != null ? `[${v.summary.len}]` : ""}${v.summary.keys ? ` keys=${v.summary.keys.slice(0,4).join(",")}…` : ""}` : "";
    const trace = v.agent_trace ? ` · attempts=${(v.agent_trace.attempts || []).length}${v.agent_trace.lesson_recorded ? " · lesson written" : ""}` : "";
    return `${k}  ←  ${v.skill}  (${v.elapsed_ms}ms · ${v.ts})  ${summary}${trace}`;
  }).join("\n");
  const failures = (p.failures || []).map(f => `  ✗ ${f.skill}: ${f.kind || ""} ${f.message || f.reason || ""}`).join("\n");
  const failBlock = failures ? `\n\nFailures handled:\n${failures}\n(replans: ${p.replan_count || 0})` : "";
  div.innerHTML = `<div class="hc-title">🧾 Sources (provenance)</div><pre class="hc-body">${escapeHtml(lines + failBlock)}</pre>`;
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

// ---------- plan card ----------
function renderPlan(evt) {
  const plan = evt.payload?.plan || (evt.kind === "planner.cached" ? null : null);
  // For cached events the plan isn't in payload — fetch from status endpoint.
  if (!plan) {
    fetchAndRenderPlanFromStatus();
    els.planSource.textContent = "retrieved from cache";
  } else {
    els.planSource.textContent = `LLM-emitted · ${plan.length} step${plan.length === 1 ? "" : "s"}`;
    drawPlan(plan);
  }
}

async function fetchAndRenderPlanFromStatus() {
  if (!state.workflowId) return;
  try {
    const r = await fetch(`/api/status/${state.workflowId}`);
    const s = await r.json();
    if (s.plan && s.plan.length) drawPlan(s.plan);
  } catch (e) { /* ignore */ }
}

function drawPlan(plan) {
  els.planCard.classList.remove("hidden");
  els.planSteps.innerHTML = "";
  for (const step of plan) {
    const li = document.createElement("li");
    li.className = "plan-step pending";
    li.dataset.skill = step.skill;
    li.innerHTML = `
      <span class="icon">${step.step}</span>
      <span><span class="skill">${escapeHtml(step.skill)}</span><span class="reason">${escapeHtml(step.reason || "")}</span></span>
      <span class="timing"></span>
    `;
    els.planSteps.appendChild(li);
  }
}

function markPlanStep(skill, status, evt) {
  const li = els.planSteps.querySelector(`[data-skill="${CSS.escape(skill)}"]`);
  if (!li) return;
  li.classList.remove("pending", "running", "done", "failed");
  li.classList.add(status);
  const icon = li.querySelector(".icon");
  if (status === "running") {
    state.planSkillStarts[skill] = Date.now();
    icon.textContent = "↻";
  } else if (status === "done") {
    icon.textContent = "✓";
    const ms = evt?.payload?.elapsed_ms;
    const t = li.querySelector(".timing");
    if (t && ms != null) t.textContent = `${(ms / 1000).toFixed(1)}s`;
  }
}

function resetPlanCard() {
  els.planCard.classList.add("hidden");
  els.planSteps.innerHTML = "";
  state.planSkillStarts = {};
}

function setSkill(name, cls) {
  const el = els.skills.find((s) => s.dataset.skill === name);
  if (el) {
    if (cls === "active") {
      els.skills.forEach((s) => s.classList.remove("active"));
      el.classList.add("active");
    } else if (cls === "done") {
      el.classList.remove("active");
      el.classList.add("done");
    }
  }
}

function resetSkillsAndCounters() {
  els.skills.forEach((s) => { s.classList.remove("active"); s.classList.remove("done"); });
  state.attempts = 0;
  state.http = 0;
  els.ctrAttempts.textContent = "0";
  els.ctrHttp.textContent = "0";
  els.ctrTokens.textContent = "–";
}

// ---------- SSE ----------
function openEventStream(wfId) {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource(`/api/events/${wfId}`);
  state.eventSource = es;
  es.onmessage = (m) => {
    try {
      const evt = JSON.parse(m.data);
      handleEvent(evt);
    } catch (e) { /* ignore */ }
  };
  // Don't change the badge on transient SSE errors — the workflow may be fine.
  es.onerror = () => { /* swallow */ };
}

function closeEventStream() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  if (state.taskQueueTimer) {
    clearInterval(state.taskQueueTimer);
    state.taskQueueTimer = null;
  }
}

// ---------- workflow status polling (for badge + completion detection) ----------
function startStatusPolling(wfId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.workflowDone = false;
  state.pollTimer = setInterval(async () => {
    try {
      const r = await fetch(`/api/status/${wfId}`);
      const s = await r.json();
      // HITL approval gating: if the workflow is paused waiting for the user,
      // surface the banner. Auto-hide once the user decides.
      if (s.pending_approval) {
        showApprovalBanner(s.pending_approval);
      } else if (state.pendingApproval) {
        hideApprovalBanner();
      }
      if (s.execution_status === "COMPLETED") {
        finishWorkflow(wfId, true);
      } else if (s.execution_status === "FAILED" || s.execution_status === "CANCELED" || s.execution_status === "TERMINATED") {
        finishWorkflow(wfId, false);
      }
    } catch (e) { /* worker may be down — keep polling */ }
  }, 2000);
}

function showApprovalBanner(skill) {
  if (state.pendingApproval === skill) return;
  state.pendingApproval = skill;
  els.approvalSkill.textContent = skill;
  els.approvalBanner.classList.remove("hidden");
}

function hideApprovalBanner() {
  state.pendingApproval = null;
  els.approvalBanner.classList.add("hidden");
}

// Worker presence: poll Temporal's task-queue pollers. The badge follows
// poller status by default, BUT once the user explicitly clicks Stop Worker
// we lock the badge to "worker disconnected" — no flapping back even if
// Temporal's poller cache briefly returns alive=true. The lock releases
// when the user clicks Start Worker.
function startTaskQueueWatcher() {
  if (state.taskQueueTimer) clearInterval(state.taskQueueTimer);
  state.consecutiveAlive = 0;
  state.consecutiveDead = 0;
  state.taskQueueTimer = setInterval(async () => {
    if (state.workflowDone) {
      clearInterval(state.taskQueueTimer);
      state.taskQueueTimer = null;
      return;
    }
    // If user explicitly stopped the worker, lock the badge until they Start.
    if (state.userStoppedWorker) return;
    // Brief grace window after Start so watcher doesn't flap to "disconnected"
    // while Temporal is still registering the new worker's pollers.
    if (state.startGraceUntil && Date.now() < state.startGraceUntil) return;

    try {
      const r = await fetch("/api/task-queue");
      const j = await r.json();
      const aliveNow = !!j.alive;

      if (aliveNow) {
        state.consecutiveAlive += 1;
        state.consecutiveDead = 0;
      } else {
        state.consecutiveDead += 1;
        state.consecutiveAlive = 0;
      }

      if (state.workerWasAlive === null) {
        state.workerWasAlive = aliveNow;
        setBadge(aliveNow ? "running" : "waiting for worker", aliveNow ? "badge-running" : "badge-frozen");
        return;
      }

      // Require 3 consecutive consistent readings before flipping (~4.5s).
      if (state.workerWasAlive && state.consecutiveDead >= 3) {
        setBadge("worker disconnected", "badge-frozen");
        state.workerWasAlive = false;
      } else if (!state.workerWasAlive && state.consecutiveAlive >= 3) {
        setBadge("resumed", "badge-running");
        state.workerWasAlive = true;
      }
    } catch (e) { /* keep last badge state on transient network errors */ }
  }, 1500);
}

async function finishWorkflow(wfId, ok) {
  if (state.workflowDone) return;
  state.workflowDone = true;
  if (state.pollTimer) clearInterval(state.pollTimer);

  if (ok) {
    setBadge("done", "badge-done");
    try {
      const r = await fetch(`/api/result/${wfId}`);
      const j = await r.json();
      const insight = j.result?.state?.report?.insight || "(no insight produced)";
      if (state.pendingMsgEl) {
        state.pendingMsgEl.textContent = insight;
        state.pendingMsgEl.classList.remove("thinking");
        state.pendingMsgEl.parentElement.className = "msg msg-agent";
      } else {
        appendChat("agent", insight);
      }
      // Fetch + render provenance so the audience can see "this number came
      // from THIS skill, ran at THIS time, with these inputs".
      try {
        const pr = await fetch(`/api/provenance/${wfId}`);
        const pj = await pr.json();
        renderProvenance(pj);
      } catch (e) { /* skip if missing */ }
    } catch (e) {
      if (state.pendingMsgEl) state.pendingMsgEl.textContent = "(could not fetch result)";
    }
  } else {
    setBadge("failed", "badge-failed");
    if (state.pendingMsgEl) {
      state.pendingMsgEl.textContent = "Workflow failed — check Temporal UI.";
      state.pendingMsgEl.parentElement.className = "msg msg-error";
    }
  }
  els.chatSend.disabled = false;
  refreshMemoryCount();
}

// ---------- health ----------
async function refreshHealth() {
  try {
    const r = await fetch("/api/health");
    const j = await r.json();
    els.hTemporal.className = "dot " + (j.temporal ? "up" : "down");
    els.hOllama.className = "dot " + (j.ollama ? "up" : "down");
    els.hMemory.textContent = j.memory_count;
  } catch (e) {
    els.hTemporal.className = "dot down";
    els.hOllama.className = "dot down";
  }
  // Also refresh worker button state from the authoritative /api/task-queue
  try {
    const r2 = await fetch("/api/task-queue");
    const tq = await r2.json();
    setWorkerButtons(!!tq.alive);
  } catch (e) {
    setWorkerButtons(false);
  }
}

async function refreshMemoryCount() {
  try {
    const r = await fetch("/api/memory");
    const j = await r.json();
    els.hMemory.textContent = j.patterns.length;
  } catch (e) {}
}

async function refreshSkillsCount() {
  try {
    const r = await fetch("/api/skills");
    const j = await r.json();
    els.hSkills.textContent = (j.skills || []).length;
  } catch (e) {}
}

// ---------- chat submission ----------
els.chatForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = els.chatInput.value.trim();
  if (!q) return;
  els.chatInput.value = "";
  els.chatSend.disabled = true;

  appendChat("you", q, { cls: "user" });

  // Routing: if we have a workflowId AND it's done, treat as warm.
  const mode = (state.workflowId && state.workflowDone) ? "warm" : "auto";

  if (mode === "warm") {
    state.pendingMsgEl = appendChat("agent", "looking up cached findings…", { cls: "pending", thinking: true });
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, mode: "warm", workflow_id: state.workflowId }),
      });
      const j = await r.json();
      state.pendingMsgEl.textContent = j.answer || "(no answer)";
      state.pendingMsgEl.classList.remove("thinking");
      state.pendingMsgEl.parentElement.className = "msg msg-agent";
    } catch (e) {
      state.pendingMsgEl.textContent = "Error: " + e.message;
      state.pendingMsgEl.parentElement.className = "msg msg-error";
    }
    els.chatSend.disabled = false;
    return;
  }

  // Cold path
  resetSkillsAndCounters();
  resetPlanCard();
  els.traceLog.innerHTML = "";
  // Auto-show the trace so the audience sees activity in real time.
  if (!document.body.classList.contains("show-trace")) {
    document.body.classList.add("show-trace");
    els.traceToggle.setAttribute("aria-pressed", "true");
    els.traceToggle.textContent = "Hide Trace";
  }
  state.pendingMsgEl = appendChat("agent", "running pipeline — live trace on the right…", { cls: "pending", thinking: true });
  setBadge("running", "badge-running");

  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        hitl: !!els.hitlToggle?.checked,
        failure_mode: els.forceFailToggle?.checked ? "drop_weather" : "",
      }),
    });
    const j = await r.json();
    state.workflowId = j.workflow_id;
    state.workerWasAlive = null;
    state.userStoppedWorker = false;  // fresh workflow — clear any prior lock
    els.wfLabel.textContent = "wf: " + j.workflow_id;
    if (j.temporal_ui) els.temporalLink.href = j.temporal_ui;
    openEventStream(j.workflow_id);
    startStatusPolling(j.workflow_id);
    startTaskQueueWatcher();

    // If user armed auto-stop, register the watcher with the server now that
    // we have a workflow id.
    // Auto-stop arms silently — UI handles the kill when weather_fetch starts.
  } catch (e) {
    state.pendingMsgEl.textContent = "Error starting workflow: " + e.message;
    state.pendingMsgEl.parentElement.className = "msg msg-error";
    setBadge("failed", "badge-failed");
    els.chatSend.disabled = false;
  }
});

async function callWorker(action) {
  const r = await fetch(`/api/worker/${action}`, { method: "POST" });
  return r.json();
}

function setWorkerButtons(alive) {
  // If the user just clicked Stop, force Start enabled / Stop disabled
  // regardless of what the task-queue cache reports — the cache lags by 12-45s.
  if (state.userStoppedWorker) {
    els.workerStopBtn.disabled = true;
    els.workerStartBtn.disabled = false;
    return;
  }
  els.workerStopBtn.disabled = !alive;
  els.workerStartBtn.disabled = alive;
}

els.autoStopBtn.addEventListener("click", () => {
  // Toggle armed state. Only takes effect on the NEXT chat submit.
  state.autoStopArmed = !state.autoStopArmed;
  els.autoStopBtn.classList.toggle("armed", state.autoStopArmed);
  els.autoStopBtn.textContent = state.autoStopArmed
    ? "⚙ Auto-stop ARMED · weather_fetch"
    : "⚙ Auto-stop @ weather_fetch";
});

els.workerStopBtn.addEventListener("click", async () => {
  if (els.workerStopBtn.disabled) return;
  if (!confirm("Stop the worker?\n\nThe in-flight activity becomes 'scheduled' in Temporal — workflow stays alive and will resume when the worker is back.")) return;
  els.workerStopBtn.disabled = true;
  els.workerStartBtn.disabled = true;
  els.workerStopBtn.classList.add("busy");
  els.workerStopBtn.textContent = "stopping…";
  // LOCK badge to "worker disconnected" — release on Start click. This stops
  // the watcher from flipping it back if Temporal's poller cache hiccups.
  state.userStoppedWorker = true;
  state.workerWasAlive = false;
  setBadge("worker disconnected", "badge-frozen");
  try {
    const j = await callWorker("stop");
    els.workerStopBtn.textContent = j.ok ? `stopped ${j.killed?.length || 0}` : "stop failed";
  } catch (e) {
    els.workerStopBtn.textContent = "stop failed";
  } finally {
    els.workerStopBtn.classList.remove("busy");
    setTimeout(() => { els.workerStopBtn.textContent = "Stop Worker"; }, 2000);
    refreshHealth();
  }
});

els.workerStartBtn.addEventListener("click", async () => {
  if (els.workerStartBtn.disabled) return;
  els.workerStartBtn.disabled = true;
  els.workerStopBtn.disabled = true;
  els.workerStartBtn.classList.add("busy");
  els.workerStartBtn.textContent = "starting…";
  // Flip badge to "resumed" immediately — the in-process worker is alive as
  // soon as the API returns. Don't wait for task-queue watcher debounce (4.5s).
  try {
    const j = await callWorker("start");
    els.workerStartBtn.textContent = j.ok ? "started" : "start failed";
    if (j.ok) {
      state.userStoppedWorker = false;
      state.workerWasAlive = true;
      state.consecutiveAlive = 3;
      state.consecutiveDead = 0;
      state.startGraceUntil = Date.now() + 8000;
      setBadge("resumed", "badge-running");
      els.workerStopBtn.disabled = false;
      els.workerStartBtn.disabled = true;
    }
  } catch (e) {
    els.workerStartBtn.textContent = "start failed";
  } finally {
    els.workerStartBtn.classList.remove("busy");
    setTimeout(() => { els.workerStartBtn.textContent = "Start Worker"; }, 2500);
  }
});

els.traceToggle.addEventListener("click", () => {
  const on = !document.body.classList.contains("show-trace");
  document.body.classList.toggle("show-trace", on);
  els.traceToggle.setAttribute("aria-pressed", on ? "true" : "false");
  els.traceToggle.textContent = on ? "Hide Trace" : "View Trace";
  if (on) {
    // jump to bottom so latest events are visible
    requestAnimationFrame(() => { els.traceLog.scrollTop = els.traceLog.scrollHeight; });
  }
});

els.resetBtn.addEventListener("click", async () => {
  if (!confirm("Wipe memory/AGENT.md and memory/learned_patterns.json?\nThe next workflow will fail-and-self-correct again, recording a fresh lesson.")) return;
  els.resetBtn.disabled = true;
  try {
    await fetch("/api/reset-memory", { method: "POST" });
    await refreshMemoryCount();
  } finally {
    els.resetBtn.disabled = false;
  }
});

// ---------- usage panel ----------
async function refreshUsage() {
  try {
    const r = await fetch("/api/usage");
    const u = await r.json();
    els.uProvider.textContent = `${u.provider} · ${u.model}`;
    els.uCalls.textContent = u.calls;
    els.uPrompt.textContent = u.prompt_tokens.toLocaleString();
    els.uCompletion.textContent = u.completion_tokens.toLocaleString();
    els.uTotal.textContent = u.total_tokens.toLocaleString();
    els.uLatency.textContent = (u.total_latency_ms / 1000).toFixed(1) + "s";
    els.uAvg.textContent = u.calls
      ? `${Math.round(u.total_tokens / u.calls)} tok / ${(u.total_latency_ms / u.calls / 1000).toFixed(1)}s`
      : "–";
    els.ctrTokens.textContent = u.total_tokens > 0 ? u.total_tokens.toLocaleString() : "–";
    els.uRecent.innerHTML = "";
    for (const c of u.last_call) {
      const li = document.createElement("li");
      li.textContent = `${c.prompt}+${c.completion} tok · ${(c.latency_ms / 1000).toFixed(2)}s`;
      els.uRecent.appendChild(li);
    }
  } catch (e) { /* ignore */ }
}

els.usageToggle.addEventListener("click", () => {
  const showing = !els.usagePanel.classList.contains("hidden");
  els.usagePanel.classList.toggle("hidden", showing);
  els.usageToggle.setAttribute("aria-pressed", showing ? "false" : "true");
  if (!showing) refreshUsage();
});
els.usageClose.addEventListener("click", () => {
  els.usagePanel.classList.add("hidden");
  els.usageToggle.setAttribute("aria-pressed", "false");
});

// ---------- HITL approve / deny ----------
async function sendDecision(decision) {
  if (!state.workflowId || !state.pendingApproval) return;
  const skill = state.pendingApproval;
  const url = decision === "approve"
    ? `/api/approve/${state.workflowId}`
    : `/api/deny/${state.workflowId}`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill }),
    });
    appendCard(
      decision === "approve" ? "✅ Approved" : "🛑 Denied",
      `Skill: ${skill}\nDecision: ${decision}`,
      decision === "approve" ? "card-ok" : "card-fail",
    );
    hideApprovalBanner();
  } catch (e) {
    appendCard("⚠ Approval signal failed", String(e.message), "card-fail");
  }
}

els.approveBtn?.addEventListener("click", () => sendDecision("approve"));
els.denyBtn?.addEventListener("click", () => sendDecision("deny"));

// ---------- reload skills ----------
els.reloadSkillsBtn?.addEventListener("click", async () => {
  if (!confirm("Stop+start the worker so newly-dropped skill files in src/skills/ get registered?")) return;
  els.reloadSkillsBtn.disabled = true;
  const original = els.reloadSkillsBtn.textContent;
  els.reloadSkillsBtn.textContent = "reloading…";
  try {
    const r = await fetch("/api/reload-skills", { method: "POST" });
    const j = await r.json();
    if (j.ok) {
      els.reloadSkillsBtn.textContent = `↻ ${j.skills.length} skills`;
      refreshSkillsCount();
    } else {
      els.reloadSkillsBtn.textContent = "reload failed";
    }
  } catch (e) {
    els.reloadSkillsBtn.textContent = "reload failed";
  } finally {
    setTimeout(() => {
      els.reloadSkillsBtn.textContent = original;
      els.reloadSkillsBtn.disabled = false;
    }, 1800);
  }
});

// Boot
refreshHealth();
refreshUsage();
refreshSkillsCount();
setInterval(refreshHealth, 4000);
setInterval(refreshUsage, 2500);
setInterval(refreshSkillsCount, 6000);

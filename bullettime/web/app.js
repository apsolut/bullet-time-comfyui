// Bullet Time web app: vanilla JS, no build step.
const $ = (id) => document.getElementById(id);
const VIEW_NAMES = {fr: "front-right", r: "right", br: "back-right", b: "back", fl: "front-left", l: "left", bl: "back-left", up: "high"};
let P = null, ARCS = {}, arc = "orbit-right", poll = null;

function ls(k, v) { try { if (v === undefined) return localStorage.getItem(k) || ""; v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch {} return ""; }
function key() { return ls("bt_key"); }
function setKey(v) { ls("bt_key", v); }
function pw() { return ls("bt_pw"); }
let HEALTH = {}, CFG = {};
// Who pays for the next paid action (same scheme as one-shot-multicam): an explicit pick wins; otherwise a saved
// key, then a saved promo code, then a free run. The server gets exactly that choice in X-Pay-Mode.
const judge = () => (CFG.judge_code ? ls("bt_judge") : "");
let judgeLeft = null;
const trialOk = () => !!(CFG.trial && (CFG.trial.runs_left > 0 || CFG.trial.used_by_you));
const freeAvail = () => trialOk() || !!(CFG.hosted && (CFG.hosted_jobs_left == null || CFG.hosted_jobs_left > 0));
function payMode() {
  const m = ls("bt_pay");
  if (m === "key" || (m === "judge" && CFG.judge_code) || (m === "free" && freeAvail())) return m;
  if (key()) return "key";
  if (judge()) return "judge";
  if (freeAvail()) return "free";
  return HEALTH.server_key ? "server" : "key";
}
const canPay = () => { const m = payMode(); return m === "key" ? !!key() : m === "judge" ? !!judge() : m === "free" ? freeAvail() : true; };
function file(path) { return `/api/projects/${P.id}/files/${path}?v=${P.render ? P.render.rendered : 0}`; }

async function api(method, url, body, isForm) {
  const h = {};
  const m = payMode();
  if (m === "key" && key()) { h["X-Comfy-Key"] = key(); h["X-Pay-Mode"] = "key"; }
  else if (m === "judge" && judge()) { h["X-Judge-Code"] = judge(); h["X-Pay-Mode"] = "promo"; }
  else if (m === "free") h["X-Pay-Mode"] = "trial";
  if (pw()) h["X-App-Password"] = pw();
  if (body && !isForm) h["Content-Type"] = "application/json";
  const r = await fetch(url, {method, headers: h, body: isForm ? body : body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

// 0 server
$("pw").value = pw();
$("pwSave").onclick = () => { try { localStorage.setItem("bt_pw", $("pw").value.trim()); } catch {} boot(); };
async function loadHealth() {
  try {
    HEALTH = await api("GET", "/api/health");
    const bits = [`v${HEALTH.version}`];
    if (HEALTH.server_key) bits.push("server has a Comfy key (yours is optional)");
    if (!HEALTH.ffmpeg) bits.push("ffmpeg missing on the server!");
    bits.push(HEALTH.eyes.ok ? "eye check on" : "eye check off");
    bits.push(`${HEALTH.max_jobs} jobs in parallel`);
    $("health").textContent = bits.join(" · ");
    $("health").title = HEALTH.eyes.ok ? "" : HEALTH.eyes.error || "";
    $("health").className = "muted small" + (!HEALTH.ffmpeg ? " warn" : "");
    $("pwRow").classList.toggle("hidden", !HEALTH.password);
  } catch (e) { $("health").textContent = e.message; }
}
async function loadRecent() {
  const box = $("recent");
  try {
    const list = await api("GET", "/api/projects?limit=12");
    box.innerHTML = "";
    $("recentCard").classList.toggle("hidden", !list.length);
    for (const r of list) {
      const a = document.createElement("a");
      a.href = `#${r.id}`;
      a.className = P && P.id === r.id ? "on" : "";
      const when = r.created ? new Date(r.created * 1000).toLocaleString() : "";
      a.textContent = `${when} · ${r.duration ? r.duration.toFixed(1) + " s" : ""}${r.arc ? " · " + r.arc : ""}${r.spent_usd ? " · $" + r.spent_usd.toFixed(2) : ""}${r.job === "running" ? " · running" : ""}`;
      a.onclick = (e) => { e.preventDefault(); open(r.id); };
      box.appendChild(a);
    }
  } catch (e) { if (e.message.includes("password")) { $("recentCard").classList.remove("hidden"); box.innerHTML = '<span class="warn">Enter the app password in the payment panel</span>'; } }
}
async function open(pid) {
  try {
    P = await api("GET", `/api/projects/${pid}`);
    history.replaceState(null, "", `#${P.id}`);
    showPick();
    if (P.job && P.job.status === "running") watch(); else showResult();
    loadRecent();
  } catch (e) { $("upErr").textContent = e.message; setSteps(); }
}

// 1 who pays
$("key").value = key();
$("judge").value = judge();
$("key").addEventListener("change", (e) => { setKey(e.target.value.trim()); if (key()) ls("bt_pay", "key"); renderAccess(); });
$("keyCheck").onclick = async () => {
  setKey($("key").value.trim()); ls("bt_pay", "key");
  keyNote("Checking…", "busy");
  try {
    const r = await fetch("/api/key/check", {method: "POST", headers: {"X-Comfy-Key": key(), "X-Pay-Mode": "key", ...(pw() ? {"X-App-Password": pw()} : {})}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    keyNote("Key works." + (j.balance_usd != null ? ` Balance: $${j.balance_usd.toFixed(2)}` : ""), "ok");
  } catch (e) { keyNote(e.message, "err"); }
  renderAccess();
};
$("judgeCheck").onclick = async () => {
  const v = $("judge").value.trim();
  keyNote("Checking the code…", "busy");
  try {
    const r = await fetch("/api/key/check", {method: "POST", headers: {"X-Judge-Code": v, ...(pw() ? {"X-App-Password": pw()} : {})}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || "That code isn't valid");
    judgeLeft = j.judge_jobs_left;
    if (!(judgeLeft > 0)) { ls("bt_judge", ""); throw new Error(j.judge_jobs_total > 0 ? "This code is used up" : "This code has expired"); }
    ls("bt_judge", v); ls("bt_pay", "judge");
    keyNote(`<b>Promo code accepted.</b> ${judgeLeft} of ${j.judge_jobs_total} jobs left on it. No Comfy key needed.`, "ok", true);
  } catch (e) { keyNote(e.message, "err"); }
  renderAccess();
};
const KEY_MSG = $("keyMsg").innerHTML;
// pay sheet status line: tone is "" (info), busy, ok, warn or err
function keyNote(msg, tone = "", html = false) {
  const el = $("keyMsg");
  el.dataset.tone = tone;
  if (!html && tone === "err" && msg) msg = msg[0].toUpperCase() + msg.slice(1);
  if (html) el.innerHTML = msg; else el.textContent = msg;
}
for (const b of document.querySelectorAll("#access button")) b.onclick = () => {
  ls("bt_pay", b.dataset.a);
  if (b.dataset.a === "key") keyNote(KEY_MSG, "", true);
  if (b.dataset.a === "judge") keyNote(judge() ? "Promo code saved in this browser. It runs a few generations on our credits, no Comfy key needed." : "Have a promo code? It runs a few generations on our credits, no Comfy key needed.");
  renderAccess();
};
// The payment panel is a sheet (right drawer, bottom sheet on phones) opened from the header pill.
function openSheet(reason) {
  if (reason) keyNote(reason, "warn");
  $("sheetBack").classList.remove("hidden"); $("paySheet").classList.remove("hidden");
  document.body.classList.add("sheet-open");
  requestAnimationFrame(() => { $("sheetBack").classList.add("in"); $("paySheet").classList.add("in"); });
  const m = payMode();
  setTimeout(() => (m === "judge" ? $("judge") : m === "key" ? $("key") : $("paySheet").querySelector("button.on") || $("sheetX")).focus(), 60);
}
function closeSheet() {
  $("sheetBack").classList.remove("in"); $("paySheet").classList.remove("in");
  document.body.classList.remove("sheet-open");
  setTimeout(() => { $("sheetBack").classList.add("hidden"); $("paySheet").classList.add("hidden"); }, 200);
}
$("payPill").onclick = () => openSheet();
$("payChange").onclick = () => openSheet();
$("sheetX").onclick = closeSheet;
$("sheetBack").onclick = closeSheet;
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("paySheet").classList.contains("hidden")) closeSheet(); });
function payLabel() {
  const m = payMode(), t = CFG.trial;
  if (m === "free") return t ? (t.used_by_you ? "Free run · in use" : `Free run · ${t.runs_left} left`) : "Demo credits";
  if (m === "judge") return judge() ? (judgeLeft != null ? `Promo code · ${judgeLeft} left` : "Promo code") : "Add a promo code";
  if (m === "key") return key() ? "Your Comfy key" : "Add a key";
  return "Server key";
}
function renderAccess() {
  const m = payMode(), t = CFG.trial;
  const lbl = payLabel();
  $("payPillT").textContent = lbl;
  $("payPill").classList.toggle("out", !canPay());
  $("payBoxT").innerHTML = canPay() ? `Paying with <b>${lbl}</b>` : `<b>${lbl}</b> to generate`;
  const hasFree = !!(t || CFG.hosted);
  $("accFreeBtn").classList.toggle("hidden", !hasFree);
  $("accJudgeBtn").classList.toggle("hidden", !CFG.judge_code);
  $("accFreeN").textContent = t ? (t.used_by_you ? "in use" : `${t.runs_left} of ${t.runs_total} left`) : CFG.hosted ? `${CFG.hosted_jobs_left ?? "∞"} jobs left` : "";
  for (const b of document.querySelectorAll("#access button")) { b.classList.toggle("on", b.dataset.a === m); b.setAttribute("aria-checked", b.dataset.a === m); }
  $("access").style.setProperty("--n", [...document.querySelectorAll("#access button")].filter((b) => !b.classList.contains("hidden")).length);
  $("accKey").classList.toggle("hidden", m === "judge" || m === "free");
  $("accJudge").classList.toggle("hidden", m !== "judge");
  if (m === "free") keyNote(t ? (t.used_by_you ? "Using your free run on our credits (one per visitor)." : `A free run on our credits: ${t.runs_left} of ${t.runs_total} left, one per visitor, up to ${t.jobs_per_run} Comfy jobs. Or bring your own key.`) : `Demo credits: ${CFG.hosted_jobs_left ?? "∞"} jobs left.`);
  if (m === "server") keyNote("This server pays with its own Comfy Cloud key.");
  updateEstimate();
  setSteps();
}

// 2 upload
const drop = $("drop");
drop.onclick = () => $("file").click();
drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file").click(); } };
function dropText(html, busy) { $("dropT").innerHTML = html; drop.classList.toggle("busy", !!busy); drop.classList.toggle("empty", !P); }
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
$("file").onchange = (e) => e.target.files[0] && upload(e.target.files[0]);

async function upload(f) {
  $("upErr").textContent = "";
  dropText(`<b>Uploading ${esc(f.name)}</b><br><small>and finding the sharpest moment with open eyes…</small>`, true);
  const fd = new FormData();
  fd.append("file", f);
  try {
    P = await api("POST", "/api/projects", fd, true);
    history.replaceState(null, "", `#${P.id}`);
    dropText(`<b>${esc(f.name)}</b> · ${P.info.duration.toFixed(1)} s<br><small>Drop another clip to start over</small>`);
    showPick();
    $("step2").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) { $("upErr").textContent = e.message; dropText('<b>Drop your video here</b> or <u>browse</u><br><small>MP4, MOV or WebM · up to 30 s</small>'); }
}

// wizard state: 1 upload, 2 moment + camera, 3 generate
function setSteps() {
  const st = (id, state, txt) => { $(id).dataset.state = state; $(id + "St").textContent = txt; };
  if (!P) { st("step1", "active", ""); st("step2", "locked", "Finish step 1"); st("step3", "locked", "Finish steps 1–2"); return; }
  st("step1", "done", "Uploaded");
  st("step2", "done", `${(ARCS[arc] || {}).label || arc} at ${P.t.toFixed(2)} s`);
  st("step3", P.render ? "done" : "active", P.render ? "Done" : "");
  const busy = P.job && P.job.status === "running";
  $("generate").disabled = !!busy;
  $("generate").textContent = busy ? "Working on it…" : P.render ? "Generate again" : "Generate bullet time";
  $("startWhy").textContent = !$("consent").checked ? "Tick the consent box in step 1 first." : !canPay() ? "Choose how to pay: a free run, your Comfy key or a promo code." : "";
}
$("consent").onchange = setSteps;
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), 3200);
}
// Promise-based confirm in a <dialog>; resolves true on Go.
function ask(title, html, yes = "Go") {
  const d = $("confirm");
  $("cTitle").textContent = title; $("cBody").innerHTML = html; $("cYes").textContent = yes;
  return new Promise((res) => { d.onclose = () => res(d.returnValue === "yes"); d.returnValue = ""; d.showModal(); });
}
const payWho = () => ({key: "of your Comfy credits", judge: "on your promo code", free: "of your free run"}[payMode()] || "on this server's credits");
const costHtml = (est, what) => `<p class="cost">≈ $${est.usd.toFixed(2)}</p><p>${what}: ${est.jobs.map(esc).join(" + ")} on Comfy Cloud, ${payWho()}.</p>`;

// 2 pick
function showPick() {
  $("pickCard").classList.remove("hidden");
  const v = $("take");
  if (v.dataset.pid !== P.id) { v.src = file(P.take); v.dataset.pid = P.id; v.onloadedmetadata = () => { v.currentTime = P.t; }; }
  $("freezeAt").innerHTML = `<span class="pp-dot" aria-hidden="true"></span>frozen at ${P.t.toFixed(2)} s`;
  $("newProject").classList.remove("hidden");
  drop.classList.remove("empty");
  drawCurve();
  drawArcs();
  updateEstimate();
  setSteps();
  const eyes = (P.analysis || {}).eyes;
  if (eyes && !eyes.ok) $("freezeAt").title = `eye check off: ${eyes.error}`;
}

function opts() {
  return {quality: $("quality").checked, variants: +$("variants").value, color_match: +$("cm").value, guide: +$("guide").value,
          fix_views: $("fixViews").checked, upscale: $("upscaleOpt").checked};
}
function updateEstimate() {
  if (!P) return;
  const o = opts(), c = HEALTH.costs_usd || {};
  const needStills = P.estimate.jobs.some((j) => j.startsWith("stills"));
  let usd = (needStills ? (o.quality ? c.stills_quality || 0.25 : c.stills || 0.045) : 0) + o.variants * (c.orbit || 0.055) + (o.upscale ? c.upscale || 0.12 : 0);
  const n = (needStills ? 1 : 0) + o.variants + (o.upscale ? 1 : 0);
  $("estimate").textContent = `≈ $${usd.toFixed(2)} · ${n} job${n > 1 ? "s" : ""}`;
  $("estimate").title = P.spent_usd ? `spent so far on this take: $${P.spent_usd.toFixed(2)}` : "";
}
for (const id of ["quality", "variants", "cm", "guide", "upscaleOpt"]) $(id).addEventListener("input", updateEstimate);
$("cm").oninput = (e) => { $("cmV").textContent = +e.target.value ? e.target.value : "off"; updateEstimate(); };
$("guide").oninput = (e) => { $("guideV").textContent = e.target.value; updateEstimate(); };

function drawCurve() {
  const c = $("curve"), g = c.getContext("2d"), a = P.analysis, W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  const xs = (t) => (t / a.duration) * W;
  const line = (arr, color) => {
    g.beginPath(); g.strokeStyle = color; g.lineWidth = 2;
    arr.forEach((v, i) => { const x = xs(a.times[i]), y = H - 8 - v * (H - 16); i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  };
  line(a.sharpness, "#333333");
  line(a.score, "#a3a3a3");
  const v = $("take");
  g.fillStyle = "#f0fe68"; g.fillRect(xs(P.t) - 2, 0, 4, H);
  if (!isNaN(v.currentTime)) { g.fillStyle = "#ffffff66"; g.fillRect(xs(v.currentTime) - 1, 0, 2, H); }
}
$("curve").onclick = (e) => {
  const r = e.target.getBoundingClientRect();
  $("take").currentTime = ((e.clientX - r.left) / r.width) * P.analysis.duration;
};
$("take").ontimeupdate = () => P && drawCurve();
$("take").onseeked = () => P && drawCurve();

async function setFreeze(t) {
  P = await api("PATCH", `/api/projects/${P.id}`, {t});
  showPick();
}
$("freezeHere").onclick = () => { const v = $("take"); v.pause(); setFreeze(v.currentTime); };
$("freezeAuto").onclick = () => { $("take").currentTime = P.analysis.t; setFreeze(P.analysis.t); };

// Top-down camera path for an arc: the subject in the middle, front at the bottom, the camera's path in the ring.
const VIEW_DEG = {real: 0, fr: 45, r: 90, br: 135, b: 180, bl: 225, l: 270, fl: 315, real_end: 360};
function arcIcon(a) {
  const cx = 50, cy = 30, R = 22, pt = (d) => [cx + R * Math.sin(d * Math.PI / 180), cy + R * Math.cos(d * Math.PI / 180)];
  const ring = `<ellipse class="cam-ring" cx="${cx}" cy="${cy}" rx="${R}" ry="${R}" fill="none" stroke-width="1.2" stroke-dasharray="2 3"/>`;
  if (a.keys.includes("up")) {  // crane: side view, camera rises from eye level to overhead
    return `<svg viewBox="0 0 100 60" aria-hidden="true"><rect x="47" y="30" width="6" height="22" rx="3" fill="#a3a3a3"/><circle cx="50" cy="24" r="5" fill="#a3a3a3"/>
      <path class="cam-path" d="M76 32 A26 26 0 0 0 52 6" fill="none" stroke-width="2.4" stroke-linecap="round"/><circle cx="76" cy="32" r="3" fill="none" stroke="#a3a3a3" stroke-width="1.5"/><circle class="cam-end" cx="52" cy="6" r="4"/></svg>`;
  }
  let prev = null; const degs = [];
  for (const k of a.keys) {
    let d = VIEW_DEG[k] ?? 0;
    if (prev != null) { while (d - prev > 180) d -= 360; while (d - prev < -180) d += 360; }
    degs.push(d); prev = d;
  }
  const pts = [];
  for (let i = 0; i < degs.length - 1; i++) for (let s = 0; s < 8; s++) pts.push(pt(degs[i] + (degs[i + 1] - degs[i]) * s / 8));
  pts.push(pt(degs[degs.length - 1]));
  const [x0, y0] = pts[0], [x1, y1] = pts[pts.length - 1];
  return `<svg viewBox="0 0 100 60" aria-hidden="true">${ring}<circle cx="${cx}" cy="${cy}" r="5" fill="#a3a3a3"/><path d="M${cx - 3} ${cy + 4}h6" stroke="#0a0a0a" stroke-width="1.4"/>
    <polyline class="cam-path" points="${pts.map((p) => p.map((v) => v.toFixed(1)).join(",")).join(" ")}" fill="none" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${x0.toFixed(1)}" cy="${y0.toFixed(1)}" r="3" fill="#141414" stroke="#a3a3a3" stroke-width="1.5"/><circle class="cam-end" cx="${x1.toFixed(1)}" cy="${y1.toFixed(1)}" r="4"/></svg>`;
}
function drawArcs() {
  const box = $("arcs");
  box.innerHTML = "";
  if (!P.arcs_ok.includes(arc)) arc = P.arcs_ok[0] || "orbit-right";
  for (const [k, a] of Object.entries(ARCS)) {
    const d = document.createElement("button");
    d.type = "button";
    d.className = "arc" + (k === arc ? " on" : "") + (P.arcs_ok.includes(k) ? "" : " off");
    d.setAttribute("role", "radio"); d.setAttribute("aria-checked", k === arc);
    const m = a.label.match(/^(.*?)\s*\((.*)\)$/) || [0, a.label, ""];
    d.innerHTML = `${arcIcon(a)}<b>${esc(m[1])}</b><small>${m[2] ? esc(m[2]) + " · " : ""}${a.freeze_s.toFixed(1)} s freeze</small>`;
    if (!P.arcs_ok.includes(k)) d.title = "Not possible for this take";
    d.onclick = () => { arc = k; drawArcs(); setSteps(); };
    box.appendChild(d);
  }
}

// 4 generate
async function generate(extra = {}) {
  if (!$("consent").checked) { $("step1").scrollIntoView({behavior: "smooth", block: "center"}); $("consent").focus(); toast("Tick the consent box first"); return; }
  if (!canPay()) {
    const m = payMode();
    openSheet(m === "judge" ? "Enter your promo code and press Use code first." : m === "free" ? "The free runs are used up. Paste your own Comfy Cloud key, or a promo code." : "Paste your Comfy Cloud key first.");
    return;
  }
  const body = {arc, subject: $("subject").value, sound: $("whoosh").checked ? "whoosh" : "silent", ...opts(), ...extra};
  try {
    const est = await api("POST", `/api/projects/${P.id}/generate`, body);
    if (!(await ask("Generate bullet time?", costHtml(est, (ARCS[arc] || {}).label || arc), "Generate"))) return;
    P = await api("POST", `/api/projects/${P.id}/generate`, {...body, confirm: true});
    watch();
  } catch (e) { $("runCard").classList.remove("hidden"); $("runErr").textContent = e.message; }
}
$("generate").onclick = () => generate();
$("reroll").onclick = () => generate();
$("newStills").onclick = () => generate({new_stills: true});
$("ramp").oninput = (e) => $("rampV").textContent = `${(+e.target.value).toFixed(1)} s`;
$("fixViewsBtn").onclick = async () => {
  try {
    const est = await api("POST", `/api/projects/${P.id}/views/retry`, {});
    if (!(await ask(`Re-roll ${est.jobs.length} rejected view${est.jobs.length > 1 ? "s" : ""}?`, costHtml(est, "View re-roll")))) return;
    P = await api("POST", `/api/projects/${P.id}/views/retry`, {confirm: true}); watch();
  } catch (e) { $("runCard").classList.remove("hidden"); $("runErr").textContent = e.message; }
};
$("upscaleBtn").onclick = async () => {
  try {
    const est = await api("POST", `/api/projects/${P.id}/upscale`, {});
    if (!(await ask("Upscale the orbit?", costHtml(est, "SeedVR2 upscale")))) return;
    P = await api("POST", `/api/projects/${P.id}/upscale`, {confirm: true}); watch();
  } catch (e) { $("runCard").classList.remove("hidden"); $("runErr").textContent = e.message; }
};
async function selectVariant(seed, arcName) {
  try { P = await api("POST", `/api/projects/${P.id}/select`, {seed, arc: arcName}); watch(); }
  catch (e) { $("runErr").textContent = e.message; }
}
$("rerender").onclick = async () => {
  try { P = await api("POST", `/api/projects/${P.id}/render`, {ramp: +$("ramp").value}); watch(); }
  catch (e) { $("runErr").textContent = e.message; }
};

function watch() {
  $("runCard").classList.remove("hidden");
  $("runErr").textContent = "";
  $("runBadge").textContent = "running"; $("runBadge").className = "badge";
  clearInterval(poll);
  const tick = async () => {
    P = await api("GET", `/api/projects/${P.id}`);
    const j = P.job || {};
    const order = ["stills", "orbit", "upscale", "render"];
    document.querySelectorAll("#steps li").forEach((s) => {
      const i = order.indexOf(s.dataset.s), cur = order.indexOf(j.step);
      s.className = j.status === "done" || (cur > i) ? "done" : cur === i && j.status === "running" ? "run" : "";
    });
    setSteps();
    const secs = j.started ? Math.round(Date.now() / 1000 - j.started) : 0;
    $("runMsg").textContent = j.status === "running" ? `${j.step}${j.comfy ? " · " + j.comfy : ""} · ${secs} s` : "";
    stageRun(j, secs);
    if (j.status === "error") { $("runErr").textContent = j.error; $("runBadge").textContent = "stopped"; $("runBadge").className = "badge warn"; clearInterval(poll); loadConfig(); }
    if (j.status === "done") { clearInterval(poll); $("runBadge").textContent = "done"; $("runBadge").className = "badge ok"; $("runCard").classList.add("hidden"); showResult(); showPick(); loadRecent(); loadConfig(); }
  };
  tick();
  poll = setInterval(tick, 2500);
}

// the stage doubles as the live render canvas while a job runs
const RUN_TITLES = {stills: "Shooting 8 new angles", orbit: "Flying the camera through frozen time", upscale: "Sharpening frozen time", render: "Splicing it back into your take"};
function stageRun(j, secs) {
  const box = $("stageRun"), on = j.status === "running" || j.status === "error";
  box.classList.toggle("hidden", !on); box.classList.toggle("err", j.status === "error");
  if (!on) { if ($("stageTag").textContent.startsWith("Render")) stageView(P && P.render ? "mine" : "ex"); return; }
  if (j.status === "running" && !(P && P.render)) stageView("ex");
  $("stageTag").textContent = j.status === "error" ? "Render stopped" : "Rendering yours";
  const order = ["stills", "orbit", "upscale", "render"], cur = order.indexOf(j.step);
  $("srK").textContent = j.status === "error" ? "Stopped" : `Step ${Math.max(cur, 0) + 1} of 4`;
  $("srT").textContent = j.status === "error" ? "Something went wrong" : RUN_TITLES[j.step] || "Rendering";
  $("srS").textContent = j.status === "error" ? "The details are in the progress card." : `${j.comfy ? j.comfy + " · " : ""}${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")} elapsed`;
  for (const i of $("srBar").children) {
    const k = order.indexOf(i.dataset.s);
    i.className = k < cur ? "done" : k === cur && j.status === "running" ? "run" : "";
  }
}

function showResult() {
  if (!P.render) return;
  $("resultCard").classList.remove("hidden");
  const v = $("result");
  v.src = file(P.render.out);
  stageView("mine");
  $("dl").href = file(P.render.out);
  $("dlLoop").href = file(P.render.orbit_only);
  $("dlWf").href = `/api/projects/${P.id}/workflows.zip`;
  $("dlExport").href = `/api/projects/${P.id}/export.zip`;
  const o = P.orbit;
  $("orbitInfo").textContent = `${ARCS[o.arc].label} · keyframes: ${o.keys.join(", ")}` + (o.dropped.length ? ` · skipped bad views: ${o.dropped.join(", ")}` : "") + ` · seed ${o.seed}` + (o.path_up ? " · upscaled" : "") + (P.spent_usd ? ` · spent $${P.spent_usd.toFixed(2)}` : "");
  $("upscaleBtnT").innerHTML = o.path_up ? "Upscaled ✓ · run again<small>SeedVR2 on the current orbit</small>" : "Upscale orbit<small>SeedVR2, sharper frozen time</small>";
  const anyBad = Object.values((P.stills || {}).views || {}).some((s) => !s.ok);
  $("fixViewsBtn").classList.toggle("hidden", !anyBad);
  const vs = P.orbits || [];
  $("variantsBox").classList.toggle("hidden", vs.length < 2);
  const vl = $("variantList");
  vl.innerHTML = "";
  for (const x of vs) {
    const d = document.createElement("div");
    d.className = "variant" + (x.path === o.path ? " on" : "");
    d.innerHTML = `<video src="${file(x.path)}" muted loop playsinline preload="metadata"></video>${esc(ARCS[x.arc] ? ARCS[x.arc].label.replace(/\s*\(.*\)$/, "") : x.arc)}<br>seed ${x.seed}${x.path_up ? " · up" : ""}`;
    d.onmouseenter = () => d.querySelector("video").play();
    d.onmouseleave = () => d.querySelector("video").pause();
    d.onclick = () => selectVariant(x.seed, x.arc);
    vl.appendChild(d);
  }
  const box = $("stills");
  box.innerHTML = "";
  for (const [k, s] of Object.entries((P.stills || {}).views || {})) {
    const f = document.createElement("figure");
    if (!s.ok) f.className = "bad";
    f.innerHTML = `<img src="${file(s.path)}" alt="${VIEW_NAMES[k] || k}" loading="lazy"><figcaption title="${esc(s.ok ? "" : s.reason)}">${VIEW_NAMES[k] || k}${s.ok ? "" : " · " + esc(s.reason)}</figcaption>`;
    box.appendChild(f);
  }
  (matchMedia("(max-width:980px)").matches ? v : $("resultCard")).scrollIntoView({behavior: "smooth", block: "start"});
}
function stageView(which) {
  const mine = which === "mine" && P && P.render;
  $("stageTabs").classList.toggle("hidden", !(P && P.render));
  for (const b of $("stageTabs").children) b.classList.toggle("on", (b.dataset.v === "mine") === !!mine);
  $("result").classList.toggle("hidden", !mine); $("stageEx").classList.toggle("hidden", !!mine);
  $("stageTag").textContent = mine ? "Your bullet time" : "Example output";
  $("stageCap").textContent = mine ? "Downloads, re-rolls and the slow-mo ramp are in the result card." : "Your take plays, time stops, the camera flies around the frozen moment, and the take carries on. Same audio, same phone shot.";
  if (mine) $("stageEx").pause(); else { $("result").pause(); $("stageEx").play().catch(() => {}); }
}
for (const b of $("stageTabs").children) b.onclick = () => stageView(b.dataset.v);

// boot
async function loadConfig() {
  try { CFG = await api("GET", "/api/config"); } catch { CFG = {}; }
  renderAccess();
}
async function boot() {
  await loadHealth();
  await loadConfig();
  try { ARCS = await api("GET", "/api/arcs"); } catch (e) { $("health").textContent = e.message; return; }
  loadRecent();
  const pid = location.hash.slice(1);
  if (pid) open(pid);
}
window.addEventListener("hashchange", () => { const pid = location.hash.slice(1); if (pid && (!P || P.id !== pid)) open(pid); });
boot();

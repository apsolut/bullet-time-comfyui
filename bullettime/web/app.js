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
    bits.push(HEALTH.eyes.ok ? "eye check on" : `eye check off: ${HEALTH.eyes.error}`);
    bits.push(`${HEALTH.max_jobs} jobs in parallel`);
    $("health").textContent = bits.join(" · ");
    $("health").className = "muted" + (!HEALTH.ffmpeg || !HEALTH.eyes.ok ? " warn" : "");
    $("pwRow").classList.toggle("hidden", !HEALTH.password);
  } catch (e) { $("health").textContent = e.message; }
}
async function loadRecent() {
  const box = $("recent");
  try {
    const list = await api("GET", "/api/projects?limit=12");
    box.innerHTML = "";
    if (!list.length) box.innerHTML = '<span class="muted">none yet</span>';
    for (const r of list) {
      const a = document.createElement("a");
      a.href = `#${r.id}`;
      a.className = P && P.id === r.id ? "on" : "";
      const when = r.created ? new Date(r.created * 1000).toLocaleString() : "";
      a.textContent = `${when} · ${r.duration ? r.duration.toFixed(1) + " s" : ""}${r.arc ? " · " + r.arc : ""}${r.spent_usd ? " · $" + r.spent_usd.toFixed(2) : ""}${r.job === "running" ? " · running" : ""}`;
      a.onclick = (e) => { e.preventDefault(); open(r.id); };
      box.appendChild(a);
    }
  } catch (e) { if (e.message.includes("password")) box.innerHTML = '<span class="warn">enter the app password above</span>'; }
}
async function open(pid) {
  try {
    P = await api("GET", `/api/projects/${pid}`);
    history.replaceState(null, "", `#${P.id}`);
    showPick();
    if (P.job && P.job.status === "running") watch(); else showResult();
    loadRecent();
  } catch (e) { $("upErr").textContent = e.message; }
}

// 1 who pays
$("key").value = key();
$("judge").value = judge();
$("key").addEventListener("change", (e) => { setKey(e.target.value.trim()); if (key()) ls("bt_pay", "key"); renderAccess(); });
$("keyCheck").onclick = async () => {
  setKey($("key").value.trim()); ls("bt_pay", "key");
  $("keyMsg").textContent = "Checking…";
  try {
    const r = await fetch("/api/key/check", {method: "POST", headers: {"X-Comfy-Key": key(), "X-Pay-Mode": "key", ...(pw() ? {"X-App-Password": pw()} : {})}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    $("keyMsg").textContent = "Key works." + (j.balance_usd != null ? ` Balance: $${j.balance_usd.toFixed(2)}` : "");
  } catch (e) { $("keyMsg").textContent = e.message; }
  renderAccess();
};
$("judgeCheck").onclick = async () => {
  const v = $("judge").value.trim();
  $("keyMsg").textContent = "Checking the code…";
  try {
    const r = await fetch("/api/key/check", {method: "POST", headers: {"X-Judge-Code": v, ...(pw() ? {"X-App-Password": pw()} : {})}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || "That code isn't valid");
    judgeLeft = j.judge_jobs_left;
    if (!(judgeLeft > 0)) { ls("bt_judge", ""); throw new Error(j.judge_jobs_total > 0 ? "This code is used up" : "This code has expired"); }
    ls("bt_judge", v); ls("bt_pay", "judge");
    $("keyMsg").textContent = `Promo code accepted: ${judgeLeft} of ${j.judge_jobs_total} jobs left on it. No Comfy key needed.`;
  } catch (e) { $("keyMsg").textContent = e.message; }
  renderAccess();
};
for (const b of document.querySelectorAll("#access button")) b.onclick = () => { ls("bt_pay", b.dataset.a); renderAccess(); };
function renderAccess() {
  const m = payMode(), t = CFG.trial;
  const hasFree = !!(t || CFG.hosted);
  $("accFreeBtn").classList.toggle("hidden", !hasFree);
  $("accJudgeBtn").classList.toggle("hidden", !CFG.judge_code);
  $("accFreeN").textContent = t ? (t.used_by_you ? "in use" : `${t.runs_left} of ${t.runs_total} left`) : CFG.hosted ? `${CFG.hosted_jobs_left ?? "∞"} jobs left` : "";
  for (const b of document.querySelectorAll("#access button")) b.classList.toggle("on", b.dataset.a === m);
  $("accKey").classList.toggle("hidden", m === "judge" || m === "free");
  $("accJudge").classList.toggle("hidden", m !== "judge");
  if (m === "free") $("keyMsg").textContent = t ? (t.used_by_you ? "Using your free run on our credits (one per visitor)." : `A free run on our credits: ${t.runs_left} of ${t.runs_total} left, one per visitor, up to ${t.jobs_per_run} Comfy jobs. Or bring your own key.`) : `Demo credits: ${CFG.hosted_jobs_left ?? "∞"} jobs left.`;
  if (m === "server") $("keyMsg").textContent = "This server pays with its own Comfy Cloud key.";
  updateEstimate();
}

// 2 upload
const drop = $("drop");
drop.onclick = () => $("file").click();
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
$("file").onchange = (e) => e.target.files[0] && upload(e.target.files[0]);

async function upload(f) {
  $("upErr").textContent = "";
  drop.textContent = `Uploading ${f.name} and finding the best moment…`;
  const fd = new FormData();
  fd.append("file", f);
  try {
    P = await api("POST", "/api/projects", fd, true);
    history.replaceState(null, "", `#${P.id}`);
    drop.textContent = `${f.name} · ${P.info.duration.toFixed(1)} s · drop another to start over`;
    showPick();
  } catch (e) { $("upErr").textContent = e.message; drop.textContent = "Drop a video here, or click to pick one."; }
}

// 3 pick
function showPick() {
  $("pickCard").classList.remove("hidden");
  const v = $("take");
  if (v.dataset.pid !== P.id) { v.src = file(P.take); v.dataset.pid = P.id; v.onloadedmetadata = () => { v.currentTime = P.t; }; }
  $("freezeAt").textContent = `freeze at ${P.t.toFixed(2)} s`;
  drawCurve();
  drawArcs();
  updateEstimate();
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
  $("estimate").textContent = `${n} Comfy job${n > 1 ? "s" : ""} ≈ $${usd.toFixed(2)}` + (P.spent_usd ? ` · spent so far $${P.spent_usd.toFixed(2)}` : "");
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
  line(a.sharpness, "#2b3a46");
  line(a.score, "#00e0a4");
  const v = $("take");
  g.fillStyle = "#7cf0ff"; g.fillRect(xs(P.t) - 1.5, 0, 3, H);
  if (!isNaN(v.currentTime)) { g.fillStyle = "#ffffff55"; g.fillRect(xs(v.currentTime) - 1, 0, 2, H); }
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

function drawArcs() {
  const box = $("arcs");
  box.innerHTML = "";
  if (!P.arcs_ok.includes(arc)) arc = P.arcs_ok[0] || "orbit-right";
  for (const [k, a] of Object.entries(ARCS)) {
    const d = document.createElement("div");
    d.className = "arc" + (k === arc ? " on" : "") + (P.arcs_ok.includes(k) ? "" : " off");
    d.innerHTML = `<b>${a.label}</b><small>${a.freeze_s.toFixed(1)} s freeze</small>`;
    d.onclick = () => { arc = k; drawArcs(); };
    box.appendChild(d);
  }
}

// 4 generate
async function generate(extra = {}) {
  if (!canPay()) {
    const m = payMode();
    $("keyMsg").textContent = m === "judge" ? "Enter your promo code and press Use code first." : m === "free" ? "The free runs are used up. Paste your own Comfy Cloud key, or a promo code." : "Paste your Comfy Cloud key first.";
    $("keyCard").scrollIntoView({behavior: "smooth"}); (m === "judge" ? $("judge") : $("key")).focus(); return;
  }
  const body = {arc, subject: $("subject").value, sound: $("whoosh").checked ? "whoosh" : "silent", ...opts(), ...extra};
  try {
    const est = await api("POST", `/api/projects/${P.id}/generate`, body);
    const who = payMode() === "key" ? "of your credits" : payMode() === "judge" ? "on your promo code" : payMode() === "free" ? "of your free run" : "on this server's credits";
    if (!confirm(`${est.jobs.join(" + ")} on Comfy Cloud ≈ $${est.usd.toFixed(2)} ${who}. Go?`)) return;
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
    if (!confirm(`Re-roll ${est.jobs.length} rejected view(s) ≈ $${est.usd.toFixed(2)}. Go?`)) return;
    P = await api("POST", `/api/projects/${P.id}/views/retry`, {confirm: true}); watch();
  } catch (e) { $("runCard").classList.remove("hidden"); $("runErr").textContent = e.message; }
};
$("upscaleBtn").onclick = async () => {
  try {
    const est = await api("POST", `/api/projects/${P.id}/upscale`, {});
    if (!confirm(`SeedVR2 upscale of the orbit ≈ $${est.usd.toFixed(2)}. Go?`)) return;
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
  clearInterval(poll);
  const tick = async () => {
    P = await api("GET", `/api/projects/${P.id}`);
    const j = P.job || {};
    const order = ["stills", "orbit", "upscale", "render"];
    document.querySelectorAll(".step").forEach((s) => {
      const i = order.indexOf(s.dataset.s), cur = order.indexOf(j.step);
      s.className = "step" + (j.status === "done" || (cur > i) ? " done" : cur === i ? " run" : "");
    });
    const secs = j.started ? Math.round(Date.now() / 1000 - j.started) : 0;
    $("runMsg").textContent = j.status === "running" ? `${j.step}${j.comfy ? " · " + j.comfy : ""} · ${secs} s` : "";
    if (j.status === "error") { $("runErr").textContent = j.error; clearInterval(poll); loadConfig(); }
    if (j.status === "done") { clearInterval(poll); showResult(); showPick(); loadRecent(); loadConfig(); }
  };
  tick();
  poll = setInterval(tick, 2500);
}

function showResult() {
  if (!P.render) return;
  $("resultCard").classList.remove("hidden");
  const v = $("result");
  v.src = file(P.render.out);
  $("dl").href = file(P.render.out);
  $("dlLoop").href = file(P.render.orbit_only);
  $("dlWf").href = `/api/projects/${P.id}/workflows.zip`;
  $("dlExport").href = `/api/projects/${P.id}/export.zip`;
  const o = P.orbit;
  $("orbitInfo").textContent = `${ARCS[o.arc].label} · keyframes: ${o.keys.join(", ")}` + (o.dropped.length ? ` · skipped bad views: ${o.dropped.join(", ")}` : "") + ` · seed ${o.seed}` + (o.path_up ? " · upscaled" : "") + (P.spent_usd ? ` · spent $${P.spent_usd.toFixed(2)}` : "");
  $("upscaleBtn").textContent = o.path_up ? "Upscaled ✓ (run again)" : "Upscale orbit (SeedVR2)";
  const anyBad = Object.values((P.stills || {}).views || {}).some((s) => !s.ok);
  $("fixViewsBtn").classList.toggle("hidden", !anyBad);
  const vs = P.orbits || [];
  $("variantsBox").classList.toggle("hidden", vs.length < 2);
  const vl = $("variantList");
  vl.innerHTML = "";
  for (const x of vs) {
    const d = document.createElement("div");
    d.className = "variant" + (x.path === o.path ? " on" : "");
    d.innerHTML = `<video src="${file(x.path)}" muted loop playsinline></video>${ARCS[x.arc] ? ARCS[x.arc].label : x.arc}<br>seed ${x.seed}${x.path_up ? " · up" : ""}`;
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
    f.innerHTML = `<img src="${file(s.path)}" alt="${VIEW_NAMES[k] || k}"><figcaption>${VIEW_NAMES[k] || k}${s.ok ? "" : " · " + s.reason}</figcaption>`;
    box.appendChild(f);
  }
  v.scrollIntoView({behavior: "smooth", block: "center"});
}

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

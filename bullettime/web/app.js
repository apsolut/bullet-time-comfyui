// Bullet Time web app: vanilla JS, no build step.
const $ = (id) => document.getElementById(id);
const VIEW_NAMES = {fr: "front-right", r: "right", br: "back-right", b: "back", fl: "front-left", l: "left", bl: "back-left", up: "high"};
let P = null, ARCS = {}, arc = "orbit-right", poll = null;

function key() { try { return localStorage.getItem("bt_key") || ""; } catch { return ""; } }
function setKey(v) { try { localStorage.setItem("bt_key", v); } catch {} }
function file(path) { return `/api/projects/${P.id}/files/${path}?v=${P.render ? P.render.rendered : 0}`; }

async function api(method, url, body, isForm) {
  const h = {};
  if (key()) h["X-Comfy-Key"] = key();
  if (body && !isForm) h["Content-Type"] = "application/json";
  const r = await fetch(url, {method, headers: h, body: isForm ? body : body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

// 1 key
$("key").value = key();
$("key").addEventListener("change", (e) => setKey(e.target.value.trim()));
$("keyCheck").onclick = async () => {
  setKey($("key").value.trim());
  $("keyMsg").textContent = "Checking…";
  try {
    const j = await api("POST", "/api/key/check");
    $("keyMsg").textContent = "Key works." + (j.balance_usd != null ? ` Balance: $${j.balance_usd.toFixed(2)}` : "");
  } catch (e) { $("keyMsg").textContent = e.message; }
};

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
  $("estimate").textContent = `${P.estimate.jobs.length} Comfy job${P.estimate.jobs.length > 1 ? "s" : ""} ≈ $${P.estimate.usd.toFixed(2)}`;
}

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
  if (!key()) { $("keyMsg").textContent = "Paste your Comfy Cloud key first."; $("key").focus(); return; }
  const body = {arc, subject: $("subject").value, sound: $("whoosh").checked ? "whoosh" : "silent", ...extra};
  try {
    const est = await api("POST", `/api/projects/${P.id}/generate`, body);
    if (!confirm(`${est.jobs.join(" + ")} on Comfy Cloud ≈ $${est.usd.toFixed(2)} of your credits. Go?`)) return;
    P = await api("POST", `/api/projects/${P.id}/generate`, {...body, confirm: true});
    watch();
  } catch (e) { $("runCard").classList.remove("hidden"); $("runErr").textContent = e.message; }
}
$("generate").onclick = () => generate();
$("reroll").onclick = () => generate();
$("newStills").onclick = () => generate({new_stills: true});
$("ramp").oninput = (e) => $("rampV").textContent = `${(+e.target.value).toFixed(1)} s`;
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
    const order = ["stills", "orbit", "render"];
    document.querySelectorAll(".step").forEach((s) => {
      const i = order.indexOf(s.dataset.s), cur = order.indexOf(j.step);
      s.className = "step" + (j.status === "done" || (cur > i) ? " done" : cur === i ? " run" : "");
    });
    const secs = j.started ? Math.round(Date.now() / 1000 - j.started) : 0;
    $("runMsg").textContent = j.status === "running" ? `${j.step}${j.comfy ? " · " + j.comfy : ""} · ${secs} s` : "";
    if (j.status === "error") { $("runErr").textContent = j.error; clearInterval(poll); }
    if (j.status === "done") { clearInterval(poll); showResult(); showPick(); }
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
  const o = P.orbit;
  $("orbitInfo").textContent = `${ARCS[o.arc].label} · keyframes: ${o.keys.join(", ")}` + (o.dropped.length ? ` · skipped bad views: ${o.dropped.join(", ")}` : "") + ` · seed ${o.seed}`;
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
(async () => {
  ARCS = await api("GET", "/api/arcs");
  const pid = location.hash.slice(1);
  if (pid) {
    try {
      P = await api("GET", `/api/projects/${pid}`);
      showPick();
      if (P.job && P.job.status === "running") watch(); else showResult();
    } catch {}
  }
})();

// Mobile VJ control. Vanilla JS, no build step.
//
// Sends actions via POST /action (fire-and-forget; the next SSE state
// push confirms the change). Receives live state via /events SSE so
// every connected phone stays in sync — when one user toggles an FX
// the others see the chip light up.

const CLIP_KEYS    = "1234567890";
const OVERLAY_KEYS = "QWERTYUIOP";
const LONG_PRESS_MS = 500;  // matches engine.LONG_PRESS_S = 0.5

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let catalog = null;     // {clips:[], overlays:[], generatives:[], fx:[]}
let state = null;       // last engine snapshot
let suppressSlider = { x: 0, y: 0 };  // ignore SSE state echoes briefly after we move a slider

// ── Action dispatch ────────────────────────────────────────────────

function postAction(name, args) {
  return fetch("/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, args: args || {} }),
    keepalive: true,
  }).catch(() => {/* offline; will reconcile on reconnect */});
}

// Generic delegated click for static buttons that carry data-action/data-args.
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const name = btn.dataset.action;
  let args = {};
  try { if (btn.dataset.args) args = JSON.parse(btn.dataset.args); } catch {}
  postAction(name, args);
});

// ── Generative + FX grids (built once from catalog) ────────────────

function buildGenGrid() {
  const grid = $("#gen-grid");
  grid.innerHTML = "";
  for (const name of catalog.generatives) {
    const b = document.createElement("button");
    b.className = "gen";
    b.dataset.gen = name;
    b.dataset.action = "select_generative";
    b.dataset.args = JSON.stringify({ name });
    b.textContent = name.toUpperCase();
    grid.appendChild(b);
  }
}

function buildFxGrid() {
  const grid = $("#fx-grid");
  grid.innerHTML = "";
  for (const name of catalog.fx) {
    const b = document.createElement("button");
    b.className = "fx";
    b.dataset.fx = name;
    b.dataset.action = "toggle_fx";
    b.dataset.args = JSON.stringify({ name });
    b.textContent = name.toUpperCase();
    grid.appendChild(b);
  }
}

// ── Favourites (tap-vs-long-press) ─────────────────────────────────

function buildFavRow(container, keys, kind) {
  container.innerHTML = "";
  for (let i = 0; i < 10; i++) {
    const cell = document.createElement("div");
    cell.className = "fav empty";
    cell.dataset.slot = String(i);
    cell.innerHTML = `<span class="k">${keys[i]}</span><span class="name"></span>`;
    attachFavHandlers(cell, kind, i);
    container.appendChild(cell);
  }
}

function attachFavHandlers(cell, kind, slot) {
  let timer = null;
  let longFired = false;

  const start = (ev) => {
    ev.preventDefault();
    longFired = false;
    timer = setTimeout(() => {
      longFired = true;
      // Long-press = assign currently-playing to this slot. Empty cells
      // become assigned; assigned cells get cleared if nothing is playing
      // (same semantics as the keyboard).
      postAction(kind === "clip" ? "save_clip_favorite" : "save_overlay_favorite",
                 { slot });
      navigator.vibrate && navigator.vibrate(40);
    }, LONG_PRESS_MS);
  };
  const end = (ev) => {
    ev.preventDefault();
    if (timer) { clearTimeout(timer); timer = null; }
    if (!longFired) {
      postAction(kind === "clip" ? "play_clip_favorite" : "play_overlay_favorite",
                 { slot });
    }
  };
  const cancel = () => {
    if (timer) { clearTimeout(timer); timer = null; }
  };

  cell.addEventListener("pointerdown", start);
  cell.addEventListener("pointerup",   end);
  cell.addEventListener("pointerleave", cancel);
  cell.addEventListener("pointercancel", cancel);
}

// ── Sliders ────────────────────────────────────────────────────────
//
// Throttle send to ~25 Hz while dragging — that's plenty for visual
// smoothness and avoids saturating the action rate-limit.

function bindSlider(input, axis) {
  let lastSent = 0;
  const send = () => {
    const v = parseFloat(input.value);
    postAction(axis === "x" ? "set_param_x" : "set_param_y", { value: v });
    // Suppress incoming SSE state for ~250ms so a stale snapshot
    // mid-drag doesn't yank the slider back.
    suppressSlider[axis] = performance.now() + 250;
    $(`#param-${axis}-val`).textContent = v.toFixed(2);
  };
  input.addEventListener("input", () => {
    const now = performance.now();
    if (now - lastSent > 40) {
      lastSent = now;
      send();
    }
  });
  input.addEventListener("change", send);   // always send the final value
}

// ── SSE rendering ──────────────────────────────────────────────────

function renderState(s) {
  state = s;

  $("#np-clip").textContent = s.clip.name
    ? `${s.clip.name}  [${s.clip.active_idx + 1}/${s.clip.total}]`
    : `—  [${s.clip.total} clip${s.clip.total === 1 ? "" : "s"}]`;
  $("#np-ovl").textContent = s.overlay.name
    ? `${s.overlay.name}  [${s.overlay.active_idx + 1}/${s.overlay.total}]`
    : "—";
  $("#np-gen").textContent = s.generative || "—";
  const fxOn = Object.entries(s.fx).filter(([, v]) => v).map(([k]) => k);
  $("#np-fx").textContent = fxOn.length ? fxOn.join(", ") : "—";

  // Active highlights
  $$(".gen").forEach((b) => b.classList.toggle("active", b.dataset.gen === s.generative));
  $$(".fx").forEach((b) => b.classList.toggle("active", !!s.fx[b.dataset.fx]));

  $("#blackout").classList.toggle("active", s.blackout);
  $("#freeze").classList.toggle("active", s.freeze);

  // Favourites — match by name (stem) like the HUD does
  const renderFavs = (containerSel, favList, activeStem) => {
    const cells = $$(`${containerSel} .fav`);
    cells.forEach((cell, i) => {
      const stem = favList[i];
      const nameEl = cell.querySelector(".name");
      nameEl.textContent = stem || "";
      cell.classList.toggle("empty", !stem);
      cell.classList.toggle("active", stem && stem === activeStem);
    });
  };
  renderFavs("#clip-favs", s.favorites.clips, s.clip.name);
  renderFavs("#overlay-favs", s.favorites.overlays, s.overlay.name);

  // Sliders — only update if user isn't actively dragging
  const now = performance.now();
  if (now > suppressSlider.x && !$("#param-x").matches(":active")) {
    $("#param-x").value = s.param_x;
    $("#param-x-val").textContent = s.param_x.toFixed(2);
  }
  if (now > suppressSlider.y && !$("#param-y").matches(":active")) {
    $("#param-y").value = s.param_y;
    $("#param-y-val").textContent = s.param_y.toFixed(2);
  }
}

function setStatus(connected) {
  const el = $("#status");
  if (connected) {
    el.textContent = "live";
    el.classList.add("connected");
    el.classList.remove("disconnected");
  } else {
    el.textContent = "reconnecting…";
    el.classList.add("disconnected");
    el.classList.remove("connected");
  }
}

function connectSSE() {
  const es = new EventSource("/events");
  es.onopen = () => setStatus(true);
  es.onerror = () => setStatus(false);
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "catalog") {
      catalog = msg.catalog;
      buildGenGrid();
      buildFxGrid();
      buildFavRow($("#clip-favs"), CLIP_KEYS, "clip");
      buildFavRow($("#overlay-favs"), OVERLAY_KEYS, "overlay");
      if (state) renderState(state);  // re-highlight now that buttons exist
    } else if (msg.type === "state") {
      renderState(msg.state);
    }
  };
}

// ── Boot ───────────────────────────────────────────────────────────

bindSlider($("#param-x"), "x");
bindSlider($("#param-y"), "y");
connectSSE();

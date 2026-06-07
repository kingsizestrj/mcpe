"use strict";

const $ = (sel) => document.querySelector(sel);

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isError);
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    window.location = "/login";
    throw new Error("não autenticado");
  }
  return res;
}

async function postJSON(path, body) {
  const res = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
  return res.json();
}

function fmtUptime(seconds) {
  if (seconds == null) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

// --------------------------------------------------------------------------- //
// Status
// --------------------------------------------------------------------------- //
async function refreshStatus() {
  try {
    const res = await api("/api/status");
    const data = await res.json();
    const pill = $("#status-pill");
    const running = data.status === "running";

    pill.textContent = data.exists ? data.status : "ausente";
    pill.className = "pill " + (running ? "pill-running" : data.exists ? "pill-stopped" : "pill-unknown");

    $("#stat-state").textContent = data.exists ? data.status : "ausente";
    $("#stat-uptime").textContent = running ? fmtUptime(data.uptime_seconds) : "—";

    const players = data.players || { online: 0, max: 0, names: [] };
    $("#stat-players").textContent = `${players.online}/${players.max || "?"}`;

    const list = $("#players-list");
    if (!players.names || players.names.length === 0) {
      list.innerHTML = '<li class="muted">Nenhum jogador online</li>';
    } else {
      list.innerHTML = "";
      players.names.forEach((name) => {
        const li = document.createElement("li");
        li.innerHTML = `<span>${name}</span>`;
        const actions = document.createElement("div");
        actions.className = "row-actions";
        const kick = document.createElement("button");
        kick.className = "btn btn-red";
        kick.textContent = "Kick";
        kick.onclick = () => kickPlayer(name);
        actions.appendChild(kick);
        li.appendChild(actions);
        list.appendChild(li);
      });
    }
  } catch (e) {
    /* silencioso */
  }
}

async function kickPlayer(name) {
  const r = await postJSON("/api/kick", { name });
  toast(r.ok ? `Kickado: ${name}` : `Erro: ${r.error || "falha"}`, !r.ok);
  setTimeout(refreshStatus, 500);
}

// --------------------------------------------------------------------------- //
// Logs / console
// --------------------------------------------------------------------------- //
async function refreshLogs() {
  try {
    const res = await api("/api/logs?tail=200");
    const data = await res.json();
    const el = $("#console");
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.textContent = data.logs || "";
    if (atBottom) el.scrollTop = el.scrollHeight;
  } catch (e) {
    /* silencioso */
  }
}

// --------------------------------------------------------------------------- //
// Allowlist
// --------------------------------------------------------------------------- //
async function refreshAllowlist() {
  try {
    const res = await api("/api/allowlist");
    const data = await res.json();
    const list = $("#allow-list");
    const entries = data.entries || [];
    if (entries.length === 0) {
      list.innerHTML = '<li class="muted">Allowlist vazia</li>';
      return;
    }
    list.innerHTML = "";
    entries.forEach((entry) => {
      const name = entry.name || entry;
      const li = document.createElement("li");
      li.innerHTML = `<span>${name}</span>`;
      const actions = document.createElement("div");
      actions.className = "row-actions";
      const rm = document.createElement("button");
      rm.className = "btn btn-red";
      rm.textContent = "Remover";
      rm.onclick = () => allowAction("remove", name);
      actions.appendChild(rm);
      li.appendChild(actions);
      list.appendChild(li);
    });
  } catch (e) {
    /* silencioso */
  }
}

async function allowAction(action, name) {
  if (!name) return;
  const r = await postJSON("/api/allowlist", { action, name });
  toast(r.ok ? `Allowlist atualizada (${action})` : `Erro: ${r.error || "falha"}`, !r.ok);
  setTimeout(refreshAllowlist, 800);
}

// --------------------------------------------------------------------------- //
// Eventos
// --------------------------------------------------------------------------- //
document.querySelectorAll("[data-action]").forEach((btn) => {
  btn.onclick = async () => {
    const action = btn.dataset.action;
    if (action === "stop" && !confirm("Parar o servidor?")) return;
    btn.disabled = true;
    const r = await postJSON(`/api/server/${action}`, {});
    btn.disabled = false;
    toast(r.ok ? `Servidor: ${action}` : `Erro: ${r.error || "falha"}`, !r.ok);
    setTimeout(refreshStatus, 1500);
    setTimeout(refreshLogs, 1500);
  };
});

document.querySelectorAll("[data-quick]").forEach((btn) => {
  btn.onclick = async () => {
    const kind = btn.dataset.quick;
    let value = btn.dataset.value;
    if (btn.dataset.from) value = $("#" + btn.dataset.from).value.trim();
    if (!value) return toast("Informe um valor", true);
    const r = await postJSON("/api/quick", { kind, value });
    toast(r.ok ? "Comando enviado" : `Erro: ${r.error || "falha"}`, !r.ok);
    if (btn.dataset.from) $("#" + btn.dataset.from).value = "";
    setTimeout(refreshLogs, 700);
  };
});

$("#cmd-send").onclick = sendCommand;
$("#cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") sendCommand(); });
async function sendCommand() {
  const input = $("#cmd");
  const command = input.value.trim();
  if (!command) return;
  const r = await postJSON("/api/command", { command });
  toast(r.ok ? "Comando enviado" : `Erro: ${r.error || "falha"}`, !r.ok);
  input.value = "";
  setTimeout(refreshLogs, 700);
}

$("#allow-add").onclick = () => {
  const name = $("#allow-name").value.trim();
  if (!name) return toast("Informe a gamertag", true);
  $("#allow-name").value = "";
  allowAction("add", name);
};

$("#btn-backup").onclick = async () => {
  toast("Gerando backup… aguarde");
  try {
    const res = await api("/api/backup", { method: "POST" });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      return toast(`Erro: ${j.error || "falha no backup"}`, true);
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const match = cd.match(/filename=([^;]+)/);
    const filename = match ? match[1].trim() : "bedrock-backup.tar.gz";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    toast("Backup baixado!");
  } catch (e) {
    toast("Erro no backup", true);
  }
};

// --------------------------------------------------------------------------- //
// Loop de atualização
// --------------------------------------------------------------------------- //
refreshStatus();
refreshLogs();
refreshAllowlist();
setInterval(refreshStatus, 8000);
setInterval(refreshLogs, 5000);
setInterval(refreshAllowlist, 20000);

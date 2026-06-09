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

    if (typeof data.allowlist_enabled === "boolean") setToggle(data.allowlist_enabled);
    if (typeof data.notify_enabled === "boolean") updateNotifyUI(data.notify_enabled);

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
  const msg = r.message || (r.ok ? `Kickado: ${name}` : `Erro: ${r.error || "falha"}`);
  toast(msg, !r.ok || /could not|no targets/i.test(r.message || ""));
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
// Allowlist — interruptor liga/desliga
// --------------------------------------------------------------------------- //
let toggleBusy = false;

function setToggle(enabled) {
  if (toggleBusy) return; // não sobrescreve enquanto o usuário aciona
  const cb = $("#allow-toggle");
  cb.checked = enabled;
  const label = $("#allow-toggle-label");
  label.textContent = enabled ? "Ligada" : "Desligada";
  label.style.color = enabled ? "#6ee787" : "var(--muted)";
}

$("#allow-toggle").addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  toggleBusy = true;
  $("#allow-toggle").disabled = true;
  try {
    const r = await postJSON("/api/allowlist/toggle", { enabled });
    if (r.ok) {
      toast(`Allowlist ${enabled ? "LIGADA" : "DESLIGADA"}`);
    } else {
      toast(`Erro: ${r.error || "falha"}`, true);
      e.target.checked = !enabled; // reverte visual
    }
  } catch (err) {
    e.target.checked = !enabled;
    toast("Erro ao alternar", true);
  } finally {
    $("#allow-toggle").disabled = false;
    toggleBusy = false;
    setToggle(e.target.checked);
  }
});

// --------------------------------------------------------------------------- //
// Allowlist — lista de jogadores
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
  // Mostra a resposta REAL do servidor quando houver (ex.: "Could not remove…").
  const msg = r.message || (r.ok ? `Allowlist atualizada (${action})` : `Erro: ${r.error || "falha"}`);
  const isErr = !r.ok || /could not|no targets|error/i.test(r.message || "");
  toast(msg, isErr);
  setTimeout(refreshAllowlist, 800);
  setTimeout(refreshSeen, 800);
}

// --------------------------------------------------------------------------- //
// Allowlist — jogadores vistos no console (com XUID, 1 clique)
// --------------------------------------------------------------------------- //
async function refreshSeen() {
  const list = $("#seen-list");
  try {
    const res = await api("/api/seen-players");
    const data = await res.json();
    const players = data.players || [];
    if (players.length === 0) {
      list.innerHTML = '<li class="muted">Ninguém visto ainda (peça para tentarem entrar)</li>';
      return;
    }
    list.innerHTML = "";
    players.forEach((p) => {
      const li = document.createElement("li");
      const info = document.createElement("div");
      info.className = "pname";
      info.innerHTML = `<span>${p.name}</span><span class="xuid">xuid: ${p.xuid}</span>`;
      li.appendChild(info);

      const actions = document.createElement("div");
      actions.className = "row-actions";
      if (p.in_allowlist) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "na lista";
        actions.appendChild(badge);
      } else {
        const add = document.createElement("button");
        add.className = "btn btn-green";
        add.textContent = "+ Adicionar";
        add.onclick = () => allowAction("add", p.name);
        actions.appendChild(add);
      }
      li.appendChild(actions);
      list.appendChild(li);
    });
  } catch (e) {
    list.innerHTML = '<li class="muted">Erro ao carregar</li>';
  }
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

$("#seen-refresh").onclick = refreshSeen;

// --------------------------------------------------------------------------- //
// Configurações do servidor (server.properties)
// --------------------------------------------------------------------------- //
async function loadProperties() {
  const form = $("#props-form");
  try {
    const res = await api("/api/properties");
    const data = await res.json();
    form.innerHTML = "";
    (data.fields || []).forEach((f) => {
      const val = (data.values || {})[f.key] ?? "";
      const wrap = document.createElement("div");
      wrap.className = "field" + (f.type === "bool" ? " field-bool" : "");
      let control;
      if (f.type === "select") {
        control = document.createElement("select");
        (f.options || []).forEach((opt) => {
          const o = document.createElement("option");
          o.value = opt; o.textContent = opt;
          if (String(val) === opt) o.selected = true;
          control.appendChild(o);
        });
      } else if (f.type === "bool") {
        control = document.createElement("input");
        control.type = "checkbox";
        control.checked = String(val).toLowerCase() === "true";
      } else {
        control = document.createElement("input");
        control.type = f.type === "number" ? "number" : "text";
        control.value = val;
      }
      control.id = "prop-" + f.key;
      control.dataset.key = f.key;
      control.dataset.type = f.type;
      const label = document.createElement("label");
      label.textContent = f.label;
      label.htmlFor = control.id;
      if (f.type === "bool") { wrap.appendChild(control); wrap.appendChild(label); }
      else { wrap.appendChild(label); wrap.appendChild(control); }
      form.appendChild(wrap);
    });
  } catch (e) {
    form.innerHTML = '<span class="muted">Erro ao carregar configurações</span>';
  }
}

function collectProperties() {
  const props = {};
  document.querySelectorAll("#props-form [data-key]").forEach((el) => {
    if (el.dataset.type === "bool") props[el.dataset.key] = el.checked ? "true" : "false";
    else props[el.dataset.key] = el.value;
  });
  return props;
}

async function saveProperties(restart) {
  if (restart && !confirm("Salvar e REINICIAR o servidor? Os jogadores cairão por alguns segundos.")) return;
  const r = await postJSON("/api/properties", { props: collectProperties(), restart });
  if (r.ok) {
    toast(restart ? "Salvo. Reiniciando servidor…" : `Salvo (${(r.changed || []).length} campos)`);
    if (restart) { setTimeout(refreshStatus, 2000); setTimeout(refreshLogs, 2500); }
  } else {
    toast(`Erro: ${r.error || "falha"}`, true);
  }
}
$("#props-save").onclick = () => saveProperties(true);
$("#props-save-norestart").onclick = () => saveProperties(false);

// --------------------------------------------------------------------------- //
// Backups
// --------------------------------------------------------------------------- //
function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(b) / Math.log(1024));
  return `${(b / Math.pow(1024, i)).toFixed(1)} ${u[i]}`;
}
function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString("pt-BR");
}

async function refreshBackups() {
  const list = $("#backup-list");
  try {
    const res = await api("/api/backups");
    const data = await res.json();
    const auto = data.auto_hours > 0
      ? `Automático: a cada ${data.auto_hours}h (mantém ${data.keep}).`
      : "Automático: desativado (defina BACKUP_INTERVAL_HOURS no .env).";
    $("#backup-info").textContent = auto;
    const backups = data.backups || [];
    if (backups.length === 0) {
      list.innerHTML = '<li class="muted">Nenhum backup ainda</li>';
      return;
    }
    list.innerHTML = "";
    backups.forEach((b) => {
      const li = document.createElement("li");
      const info = document.createElement("div");
      info.className = "bname";
      info.innerHTML = `<span>${b.name}</span><span class="backup-meta">${fmtDate(b.mtime)} · ${fmtBytes(b.size)}</span>`;
      li.appendChild(info);
      const actions = document.createElement("div");
      actions.className = "row-actions";
      const dl = document.createElement("a");
      dl.className = "btn"; dl.textContent = "⬇";
      dl.href = `/api/backups/download?name=${encodeURIComponent(b.name)}`;
      const rs = document.createElement("button");
      rs.className = "btn btn-yellow"; rs.textContent = "Restaurar";
      rs.onclick = () => restoreBackup(b.name);
      const del = document.createElement("button");
      del.className = "btn btn-red"; del.textContent = "🗑";
      del.onclick = () => deleteBackup(b.name);
      actions.append(dl, rs, del);
      li.appendChild(actions);
      list.appendChild(li);
    });
  } catch (e) {
    list.innerHTML = '<li class="muted">Erro ao carregar</li>';
  }
}

async function restoreBackup(name) {
  if (!confirm(`RESTAURAR "${name}"?\n\nIsto substitui o mundo atual e reinicia o servidor. Um backup de segurança do estado atual é criado antes.`)) return;
  toast("Restaurando… aguarde");
  const r = await postJSON("/api/backups/restore", { name });
  toast(r.ok ? "Backup restaurado!" : `Erro: ${r.error || "falha"}`, !r.ok);
  setTimeout(refreshStatus, 2500);
  setTimeout(refreshBackups, 2500);
}

async function deleteBackup(name) {
  if (!confirm(`Apagar o backup "${name}"?`)) return;
  const r = await postJSON("/api/backups/delete", { name });
  toast(r.ok ? "Backup apagado" : `Erro: ${r.error || "falha"}`, !r.ok);
  refreshBackups();
}

$("#backup-create").onclick = async () => {
  toast("Criando backup… aguarde");
  const r = await postJSON("/api/backups/create", {});
  toast(r.ok ? `Backup criado: ${r.name}` : `Erro: ${r.error || "falha"}`, !r.ok);
  refreshBackups();
};

// --------------------------------------------------------------------------- //
// Recursos (CPU/RAM)
// --------------------------------------------------------------------------- //
function setBar(barId, valId, pct, label) {
  const bar = $(barId);
  const v = Math.max(0, Math.min(100, pct || 0));
  bar.style.width = v + "%";
  bar.classList.toggle("warn", v >= 70 && v < 90);
  bar.classList.toggle("crit", v >= 90);
  $(valId).textContent = label;
}

async function refreshStats() {
  try {
    const res = await api("/api/stats");
    const data = await res.json();
    const c = data.current || {};
    if (c.cpu == null) {
      setBar("#res-cpu-bar", "#res-cpu-val", 0, "—");
      setBar("#res-mem-bar", "#res-mem-val", 0, "—");
    } else {
      setBar("#res-cpu-bar", "#res-cpu-val", c.cpu, `${c.cpu}%`);
      const memLabel = c.limit_mb
        ? `${c.mem_pct}% (${c.mem_mb}/${c.limit_mb} MB)`
        : `${c.mem_mb} MB`;
      setBar("#res-mem-bar", "#res-mem-val", c.mem_pct, memLabel);
    }
    const spark = $("#res-spark");
    spark.innerHTML = "";
    (data.history || []).slice(-60).forEach((h) => {
      const b = document.createElement("span");
      b.style.height = Math.max(2, Math.min(100, h.cpu)) + "%";
      spark.appendChild(b);
    });
  } catch (e) {
    /* silencioso */
  }
}

// --------------------------------------------------------------------------- //
// Manutenção: versão + notificações
// --------------------------------------------------------------------------- //
async function loadVersion() {
  try {
    const res = await api("/api/version");
    const d = await res.json();
    const el = $("#ver-text");
    if (!d.current) {
      el.textContent = "desconhecida (servidor parado?)";
    } else if (d.update_available) {
      el.innerHTML = `${d.current} → <span class="ver-new">${d.latest} disponível</span>`;
    } else if (d.latest) {
      el.textContent = `${d.current} (atualizada)`;
    } else {
      el.textContent = `${d.current}`;
    }
    $("#ver-update").hidden = false;
  } catch (e) {
    $("#ver-text").textContent = "erro ao verificar";
  }
}

$("#ver-update").onclick = async () => {
  if (!confirm("Atualizar o servidor?\n\nFaz um backup, reinicia o container e baixa a última versão. Os jogadores cairão por ~1 min.")) return;
  toast("Atualizando… backup + reinício");
  const r = await postJSON("/api/version/update", {});
  toast(r.ok ? "Reiniciando para aplicar a atualização…" : `Erro: ${r.error || "falha"}`, !r.ok);
  setTimeout(refreshStatus, 3000);
  setTimeout(loadVersion, 8000);
};

function updateNotifyUI(enabled) {
  $("#notify-text").textContent = enabled
    ? "configurado ✓"
    : "desativado (configure no .env)";
  $("#notify-test").disabled = !enabled;
}

$("#notify-test").onclick = async () => {
  const r = await postJSON("/api/notify/test", {});
  toast(r.ok ? "Notificação enviada!" : `Erro: ${r.error || "falha"}`, !r.ok);
};

// --------------------------------------------------------------------------- //
// Loop de atualização
// --------------------------------------------------------------------------- //
refreshStatus();
refreshLogs();
refreshAllowlist();
refreshSeen();
loadProperties();
refreshBackups();
refreshStats();
loadVersion();
setInterval(refreshStatus, 8000);
setInterval(refreshLogs, 5000);
setInterval(refreshAllowlist, 20000);
setInterval(refreshSeen, 30000);
setInterval(refreshBackups, 30000);
setInterval(refreshStats, 15000);

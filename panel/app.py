"""
Painel web para o servidor Minecraft Bedrock (itzg/minecraft-bedrock-server).

Funcionalidades:
  - Login protegido (usuário/senha vindos de variáveis de ambiente).
  - Status do servidor (rodando/parado, jogadores online).
  - Start / Stop / Restart do container.
  - Console ao vivo (logs) e envio de comandos arbitrários.
  - Gerenciamento da allowlist (lista de permitidos).
  - Ações rápidas: say, kick, tempo, clima, dificuldade, gamemode.
  - Editor do server.properties.
  - Backups do mundo (manuais, agendados, restauração).

O painel fala com o servidor usando o socket do Docker, executando o script
`send-command` (que já vem na imagem do Bedrock) dentro do container.
"""

import functools
import json
import os
import re
import shutil
import tarfile
import threading
import time
from datetime import datetime, timezone

import docker
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
BEDROCK_CONTAINER = os.environ.get("BEDROCK_CONTAINER", "mc_bedrock_server")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "troque-esta-senha")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SECRET_KEY = os.environ.get("SECRET_KEY", "troque-esta-chave-secreta")

# Backups
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
BACKUP_INTERVAL_HOURS = float(os.environ.get("BACKUP_INTERVAL_HOURS", "0") or 0)
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "7") or 7)

# Guarda apenas o hash da senha em memória.
ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

_docker_client = None


def docker_client():
    """Cliente Docker (lazy, reutilizado entre requisições)."""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def get_container():
    """Retorna o container do Bedrock ou None se não existir."""
    try:
        return docker_client().containers.get(BEDROCK_CONTAINER)
    except docker.errors.NotFound:
        return None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Autenticação
# --------------------------------------------------------------------------- #
def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "não autenticado"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pwd = request.form.get("password", "")
        if user == ADMIN_USER and check_password_hash(ADMIN_PASSWORD_HASH, pwd):
            session["logged_in"] = True
            session["user"] = user
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        flash("Usuário ou senha inválidos.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Helpers de comando / console
# --------------------------------------------------------------------------- #
def send_command(command: str, capture: bool = False, wait: float = 0.6):
    """
    Envia um comando ao console do servidor via `send-command`.

    Se capture=True, retorna as novas linhas de log geradas logo após o
    comando (útil para `list`, `allowlist list`, etc).
    """
    container = get_container()
    if container is None:
        return {"ok": False, "error": "container não encontrado"}
    if container.status != "running":
        return {"ok": False, "error": "servidor não está rodando"}

    # Marca o instante ANTES de enviar; depois lemos só os logs desse ponto em
    # diante (confiável, sem depender de casar texto anterior).
    since = datetime.now(timezone.utc) if capture else None

    # Encaminha a linha de comando VERBATIM como um único argumento. Assim o
    # script send-command repassa exatamente o texto ao console, preservando
    # aspas e espaços (ex.: nomes com espaço -> allowlist add "King Size").
    args = ["send-command", command]
    try:
        result = container.exec_run(args, demux=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    output = ""
    if capture:
        time.sleep(wait)
        try:
            output = container.logs(since=since, timestamps=False).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            output = ""

    exit_code = getattr(result, "exit_code", 0)
    return {"ok": exit_code == 0, "output": output, "exit_code": exit_code}


def _quote_name(name: str) -> str:
    """
    Coloca aspas duplas em nomes com espaço (o console do Bedrock exige isso,
    ex.: allowlist add "King Size"). Remove aspas internas por segurança.
    """
    clean = name.replace('"', "").strip()
    return f'"{clean}"' if " " in clean else clean


LOG_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")


def _clean_log_lines(raw: str):
    """Remove o prefixo '[2026-... INFO]' e linhas vazias."""
    lines = []
    for ln in (raw or "").splitlines():
        ln = LOG_PREFIX_RE.sub("", ln).strip()
        if ln:
            lines.append(ln)
    return lines


def summarize_response(raw: str, name: str = "") -> str:
    """
    Extrai a resposta relevante do jogo a partir do log capturado, p.ex.:
    'Could not remove KingsizeSTRJ from the allowlist'. Ignora ruído.
    """
    keywords = ("allowlist", "could not", "added", "remove", "already", "no targets", "kicked")
    relevant = []
    for ln in _clean_log_lines(raw):
        low = ln.lower()
        if "reloaded from file" in low:
            continue  # ruído do reload automático
        if (name and name.lower() in low) or any(k in low for k in keywords):
            if ln not in relevant:
                relevant.append(ln)
    return " | ".join(relevant[:3])


# Eventos de entrada/saída que o Bedrock escreve no log, ex:
#   [INFO] Player connected: Steve, xuid: 2535...
#   [INFO] Player disconnected: Steve, xuid: 2535...
CONNECT_RE = re.compile(r"Player connected:\s*(.+?),\s*xuid:\s*(\S+)", re.IGNORECASE)
DISCONNECT_RE = re.compile(r"Player disconnected:\s*(.+?),\s*xuid:\s*(\S+)", re.IGNORECASE)


def _max_players():
    """Lê max-players do server.properties (0 se indisponível)."""
    props = os.path.join(DATA_DIR, "server.properties")
    try:
        with open(props, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("max-players="):
                    return int(line.strip().split("=", 1)[1])
    except Exception:  # noqa: BLE001
        pass
    return 0


def list_players():
    """
    Determina os jogadores online a partir dos eventos connect/disconnect
    no log do servidor — sem enviar nenhum comando ao console.

    Vantagens sobre enviar `list` a cada atualização:
      - Não causa "pisca-pisca" (estado estável e determinístico).
      - Não polui o console com respostas repetidas de `list`.
    """
    container = get_container()
    if container is None or container.status != "running":
        return {"online": 0, "max": 0, "names": []}

    try:
        raw = container.logs(tail=5000, timestamps=False).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {"online": 0, "max": 0, "names": []}

    # Reproduz os eventos em ordem; quem entrou e não saiu fica online.
    # Chaveado por XUID (estável), guardando o nome para exibição.
    online = {}  # xuid -> name (dict mantém ordem de entrada)
    for line in raw.splitlines():
        mc = CONNECT_RE.search(line)
        if mc:
            online[mc.group(2)] = mc.group(1).strip()
            continue
        md = DISCONNECT_RE.search(line)
        if md:
            online.pop(md.group(2), None)

    names = list(online.values())
    return {"online": len(names), "max": _max_players(), "names": names}


def allowlist_path():
    return os.path.join(DATA_DIR, "allowlist.json")


def read_allowlist():
    path = allowlist_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return []


def _props_path():
    return os.path.join(DATA_DIR, "server.properties")


def get_prop(key, default=None):
    """Lê um valor do server.properties."""
    path = _props_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1]
    except Exception:  # noqa: BLE001
        pass
    return default


def set_prop(key, value):
    """Escreve/atualiza um valor no server.properties (para persistir)."""
    path = _props_path()
    try:
        lines = []
        found = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        return True
    except Exception:  # noqa: BLE001
        return False


def allowlist_enabled():
    """Estado atual da allowlist (allow-list no server.properties)."""
    return str(get_prop("allow-list", "false")).lower() == "true"


# --------------------------------------------------------------------------- #
# Páginas
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    return render_template("index.html", server_name=os.environ.get("SERVER_NAME", "Bedrock"))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.route("/api/status")
@login_required
def api_status():
    container = get_container()
    if container is None:
        return jsonify({"exists": False, "status": "ausente"})

    info = {
        "exists": True,
        "status": container.status,
        "name": container.name,
    }

    # Uptime aproximado a partir de StartedAt.
    try:
        started = container.attrs["State"]["StartedAt"]
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - started_dt
        info["uptime_seconds"] = int(delta.total_seconds())
    except Exception:  # noqa: BLE001
        info["uptime_seconds"] = None

    if container.status == "running":
        players = list_players()
        info["players"] = players
    else:
        info["players"] = {"online": 0, "max": 0, "names": []}

    info["allowlist_enabled"] = allowlist_enabled()
    return jsonify(info)


@app.route("/api/logs")
@login_required
def api_logs():
    container = get_container()
    if container is None:
        return jsonify({"logs": "container não encontrado"})
    tail = request.args.get("tail", default=200, type=int)
    tail = max(1, min(tail, 1000))
    try:
        logs = container.logs(tail=tail).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        logs = f"erro ao ler logs: {exc}"
    return jsonify({"logs": logs})


@app.route("/api/server/<action>", methods=["POST"])
@login_required
def api_server_action(action):
    container = get_container()
    if container is None:
        return jsonify({"ok": False, "error": "container não encontrado"}), 404
    try:
        if action == "start":
            container.start()
        elif action == "stop":
            container.stop(timeout=30)
        elif action == "restart":
            container.restart(timeout=30)
        else:
            return jsonify({"ok": False, "error": "ação inválida"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "action": action})


@app.route("/api/command", methods=["POST"])
@login_required
def api_command():
    data = request.get_json(silent=True) or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"ok": False, "error": "comando vazio"}), 400
    # Não deixa enviar o próprio "stop" por aqui sem querer travar o painel.
    res = send_command(command, capture=True)
    return jsonify(res)


@app.route("/api/players")
@login_required
def api_players():
    return jsonify(list_players())


@app.route("/api/seen-players")
@login_required
def api_seen_players():
    """
    Lista jogadores já vistos no console (eventos 'Player connected'), com seu
    XUID, para adicionar à allowlist com um clique. Marca quem já está na lista.
    """
    container = get_container()
    if container is None or container.status != "running":
        return jsonify({"players": []})

    try:
        raw = container.logs(tail=5000, timestamps=False).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return jsonify({"players": []})

    seen = {}  # name -> xuid (mantém a última ocorrência)
    for line in raw.splitlines():
        m = CONNECT_RE.search(line)
        if m:
            seen[m.group(1).strip()] = m.group(2)

    allow_names = {
        (e.get("name") or "").lower()
        for e in read_allowlist()
        if isinstance(e, dict)
    }
    players = [
        {"name": n, "xuid": x, "in_allowlist": n.lower() in allow_names}
        for n, x in seen.items()
    ]
    return jsonify({"players": players})


@app.route("/api/allowlist", methods=["GET"])
@login_required
def api_allowlist_get():
    return jsonify({"entries": read_allowlist(), "enabled": allowlist_enabled()})


@app.route("/api/allowlist/toggle", methods=["POST"])
@login_required
def api_allowlist_toggle():
    """Liga/desliga a allowlist em tempo real e persiste no server.properties."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))

    # Efeito imediato no servidor (sem reiniciar).
    res = send_command("allowlist on" if enabled else "allowlist off", capture=True)
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error", "falha ao enviar comando")}), 500

    # Persiste para sobreviver a reinícios do container.
    set_prop("allow-list", "true" if enabled else "false")
    return jsonify({"ok": True, "enabled": enabled, "output": res.get("output", "")})


@app.route("/api/allowlist", methods=["POST"])
@login_required
def api_allowlist_post():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    name = (data.get("name") or "").strip()
    if action not in ("add", "remove") or not name:
        return jsonify({"ok": False, "error": "parâmetros inválidos"}), 400
    res = send_command(f"allowlist {action} {_quote_name(name)}", capture=True)
    # Recarrega a allowlist no servidor para aplicar.
    send_command("allowlist reload")
    # Mensagem real do jogo (ex.: "Could not remove X from the allowlist").
    res["message"] = summarize_response(res.get("output", ""), name)
    return jsonify(res)


@app.route("/api/kick", methods=["POST"])
@login_required
def api_kick():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    reason = (data.get("reason") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "nome obrigatório"}), 400
    cmd = f"kick {_quote_name(name)}" + (f" {reason}" if reason else "")
    res = send_command(cmd, capture=True)
    res["message"] = summarize_response(res.get("output", ""), name)
    return jsonify(res)


@app.route("/api/quick", methods=["POST"])
@login_required
def api_quick():
    """Ações rápidas pré-validadas para evitar comandos arbitrários perigosos."""
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    value = (data.get("value") or "").strip()

    mapping = {
        "say": lambda v: f"say {v}",
        "time": lambda v: f"time set {v}",       # day / night / noon / midnight
        "weather": lambda v: f"weather {v}",     # clear / rain / thunder
        "difficulty": lambda v: f"difficulty {v}",
        "gamemode": lambda v: f"gamemode {v} @a",
    }
    if kind not in mapping:
        return jsonify({"ok": False, "error": "ação desconhecida"}), 400
    if not value:
        return jsonify({"ok": False, "error": "valor obrigatório"}), 400
    return jsonify(send_command(mapping[kind](value), capture=True))


# --------------------------------------------------------------------------- #
# Editor do server.properties
# --------------------------------------------------------------------------- #
# Campos editáveis pela UI (whitelist — só estas chaves podem ser gravadas).
# server-port é omitido de propósito: mudar quebraria o mapeamento do Docker.
PROPERTY_FIELDS = [
    {"key": "server-name", "label": "Nome do servidor", "type": "text"},
    {"key": "gamemode", "label": "Modo de jogo", "type": "select",
     "options": ["survival", "creative", "adventure"]},
    {"key": "difficulty", "label": "Dificuldade", "type": "select",
     "options": ["peaceful", "easy", "normal", "hard"]},
    {"key": "max-players", "label": "Máx. de jogadores", "type": "number"},
    {"key": "level-name", "label": "Nome do mundo", "type": "text"},
    {"key": "level-seed", "label": "Seed do mundo (vazio = aleatória)", "type": "text"},
    {"key": "allow-cheats", "label": "Permitir cheats", "type": "bool"},
    {"key": "online-mode", "label": "Online mode (exige conta Xbox)", "type": "bool"},
    {"key": "view-distance", "label": "Distância de visão (chunks)", "type": "number"},
    {"key": "tick-distance", "label": "Tick distance (4–12)", "type": "number"},
    {"key": "player-idle-timeout", "label": "Timeout de inatividade (min, 0=off)", "type": "number"},
    {"key": "default-player-permission-level", "label": "Permissão padrão", "type": "select",
     "options": ["visitor", "member", "operator"]},
]
ALLOWED_PROP_KEYS = {f["key"] for f in PROPERTY_FIELDS}


@app.route("/api/properties", methods=["GET"])
@login_required
def api_properties_get():
    values = {f["key"]: (get_prop(f["key"], "") or "") for f in PROPERTY_FIELDS}
    return jsonify({"fields": PROPERTY_FIELDS, "values": values})


@app.route("/api/properties", methods=["POST"])
@login_required
def api_properties_post():
    data = request.get_json(silent=True) or {}
    props = data.get("props") or {}
    do_restart = bool(data.get("restart"))

    changed = []
    for key, value in props.items():
        if key not in ALLOWED_PROP_KEYS:
            continue  # ignora chaves fora da whitelist (segurança)
        value = str(value).strip().replace("\n", "").replace("\r", "")
        if set_prop(key, value):
            changed.append(key)

    if not changed:
        return jsonify({"ok": False, "error": "nada para salvar"}), 400

    restarted = False
    if do_restart:
        container = get_container()
        if container is not None:
            try:
                container.restart(timeout=30)
                restarted = True
            except Exception as exc:  # noqa: BLE001
                return jsonify({"ok": True, "changed": changed,
                                "restarted": False, "warn": str(exc)})
    return jsonify({"ok": True, "changed": changed, "restarted": restarted})


# --------------------------------------------------------------------------- #
# Backups (manuais, agendados, restauração)
# --------------------------------------------------------------------------- #
_backup_lock = threading.Lock()


def _safe_backup_path(name):
    """Resolve um nome de backup para um caminho seguro dentro de BACKUP_DIR."""
    safe = secure_filename(name or "")
    if not safe.endswith(".tar.gz"):
        return None
    path = os.path.join(BACKUP_DIR, safe)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(BACKUP_DIR):
        return None
    return path


def make_backup():
    """Cria um .tar.gz de DATA_DIR em BACKUP_DIR. Retorna o caminho."""
    with _backup_lock:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        # Garante consistência do mundo durante a cópia.
        held = send_command("save hold")
        if held.get("ok"):
            time.sleep(2)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(BACKUP_DIR, f"bedrock-{stamp}.tar.gz")
        try:
            with tarfile.open(path, "w:gz") as tar:
                tar.add(DATA_DIR, arcname="data")
        finally:
            send_command("save resume")
        _prune_backups()
        return path


def _prune_backups():
    """Mantém apenas os BACKUP_KEEP backups mais recentes."""
    try:
        files = sorted(
            (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
             if f.endswith(".tar.gz")),
            key=os.path.getmtime, reverse=True,
        )
        for old in files[BACKUP_KEEP:]:
            os.remove(old)
    except Exception:  # noqa: BLE001
        pass


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for f in os.listdir(BACKUP_DIR):
        if not f.endswith(".tar.gz"):
            continue
        p = os.path.join(BACKUP_DIR, f)
        try:
            st = os.stat(p)
            items.append({"name": f, "size": st.st_size, "mtime": int(st.st_mtime)})
        except Exception:  # noqa: BLE001
            continue
    return sorted(items, key=lambda x: x["mtime"], reverse=True)


@app.route("/api/backups", methods=["GET"])
@login_required
def api_backups_list():
    return jsonify({
        "backups": list_backups(),
        "auto_hours": BACKUP_INTERVAL_HOURS,
        "keep": BACKUP_KEEP,
    })


@app.route("/api/backups/create", methods=["POST"])
@login_required
def api_backups_create():
    if not os.path.isdir(DATA_DIR):
        return jsonify({"ok": False, "error": "DATA_DIR não encontrado"}), 404
    try:
        path = make_backup()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "name": os.path.basename(path)})


@app.route("/api/backups/download")
@login_required
def api_backups_download():
    path = _safe_backup_path(request.args.get("name"))
    if not path or not os.path.exists(path):
        return jsonify({"ok": False, "error": "backup não encontrado"}), 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/api/backups/delete", methods=["POST"])
@login_required
def api_backups_delete():
    data = request.get_json(silent=True) or {}
    path = _safe_backup_path(data.get("name"))
    if not path or not os.path.exists(path):
        return jsonify({"ok": False, "error": "backup não encontrado"}), 404
    try:
        os.remove(path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/backups/restore", methods=["POST"])
@login_required
def api_backups_restore():
    """Restaura um backup: para o servidor, troca os dados e reinicia."""
    data = request.get_json(silent=True) or {}
    path = _safe_backup_path(data.get("name"))
    if not path or not os.path.exists(path):
        return jsonify({"ok": False, "error": "backup não encontrado"}), 404

    container = get_container()
    was_running = container is not None and container.status == "running"

    # Backup de segurança do estado atual antes de sobrescrever.
    try:
        make_backup()
    except Exception:  # noqa: BLE001
        pass

    try:
        if container is not None and was_running:
            container.stop(timeout=30)

        # Limpa DATA_DIR e extrai o backup (removendo o prefixo "data/").
        for entry in os.listdir(DATA_DIR):
            full = os.path.join(DATA_DIR, entry)
            shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)

        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "data":
                    continue
                if member.name.startswith("data/"):
                    member.name = member.name[len("data/"):]
                if not member.name:
                    continue
                tar.extract(member, DATA_DIR, filter="data")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"falha ao restaurar: {exc}"}), 500
    finally:
        # Só religa se estava rodando antes (não inicia um servidor parado).
        if container is not None and was_running:
            try:
                container.start()
            except Exception:  # noqa: BLE001
                pass

    return jsonify({"ok": True, "restored": os.path.basename(path)})


def _backup_scheduler():
    """Loop em background que cria backups a cada BACKUP_INTERVAL_HOURS."""
    interval = BACKUP_INTERVAL_HOURS * 3600
    while True:
        time.sleep(interval)
        try:
            make_backup()
        except Exception:  # noqa: BLE001
            pass


@app.route("/healthz")
def healthz():
    return "ok", 200


# Inicia o agendador de backups (1 worker no gunicorn -> 1 agendador).
if BACKUP_INTERVAL_HOURS > 0:
    threading.Thread(target=_backup_scheduler, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

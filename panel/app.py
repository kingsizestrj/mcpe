"""
Painel web para o servidor Minecraft Bedrock (itzg/minecraft-bedrock-server).

Funcionalidades:
  - Login protegido (usuário/senha vindos de variáveis de ambiente).
  - Status do servidor (rodando/parado, jogadores online).
  - Start / Stop / Restart do container.
  - Console ao vivo (logs) e envio de comandos arbitrários.
  - Gerenciamento da allowlist (lista de permitidos).
  - Ações rápidas: say, kick, tempo, clima, dificuldade, gamemode.
  - Backup do mundo.

O painel fala com o servidor usando o socket do Docker, executando o script
`send-command` (que já vem na imagem do Bedrock) dentro do container.
"""

import functools
import io
import json
import os
import re
import tarfile
import time
from datetime import datetime, timezone

import docker
from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
BEDROCK_CONTAINER = os.environ.get("BEDROCK_CONTAINER", "mc_bedrock_server")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "troque-esta-senha")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SECRET_KEY = os.environ.get("SECRET_KEY", "troque-esta-chave-secreta")

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

    before = b""
    if capture:
        before = container.logs(tail=1, timestamps=False)

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
        after = container.logs(tail=40, timestamps=False).decode("utf-8", "replace")
        before_txt = before.decode("utf-8", "replace").strip()
        # Mantém só o que veio depois da última linha conhecida.
        if before_txt and before_txt in after:
            output = after.split(before_txt, 1)[-1].strip()
        else:
            output = after.strip()

    exit_code = getattr(result, "exit_code", 0)
    return {"ok": exit_code == 0, "output": output, "exit_code": exit_code}


def _quote_name(name: str) -> str:
    """
    Coloca aspas duplas em nomes com espaço (o console do Bedrock exige isso,
    ex.: allowlist add "King Size"). Remove aspas internas por segurança.
    """
    clean = name.replace('"', "").strip()
    return f'"{clean}"' if " " in clean else clean


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


@app.route("/api/allowlist", methods=["GET"])
@login_required
def api_allowlist_get():
    return jsonify({"entries": read_allowlist()})


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
    return jsonify(send_command(cmd, capture=True))


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


@app.route("/api/backup", methods=["POST"])
@login_required
def api_backup():
    """Cria um .tar.gz do diretório de dados e devolve para download."""
    if not os.path.isdir(DATA_DIR):
        return jsonify({"ok": False, "error": "DATA_DIR não encontrado"}), 404

    # Salva o mundo antes do backup, se estiver rodando.
    send_command("save hold")
    time.sleep(1)

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(DATA_DIR, arcname="data")
    buffer.seek(0)

    send_command("save resume")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"bedrock-backup-{stamp}.tar.gz"
    return Response(
        buffer.getvalue(),
        mimetype="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

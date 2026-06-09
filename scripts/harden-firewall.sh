#!/usr/bin/env bash
# ===========================================================================
# Blindagem de firewall para o servidor Minecraft Bedrock exposto na internet.
#
# Adiciona proteção no nível do host (iptables) para a porta UDP do jogo:
#   - Descarta pacotes inválidos (scan/malformados).
#   - Limita a taxa de pacotes POR IP de origem (mitiga flood/DDoS leve).
#   - (Opcional) Restringe o acesso só a IPs conhecidos (ALLOW_IPS).
#
# As regras vão para uma chain própria (MC_HARDEN) acionada a partir da
# DOCKER-USER, que é o lugar correto para filtrar tráfego de containers Docker.
#
# IMPORTANTE: isto NÃO substitui a allowlist + online mode do jogo (que são a
# autenticação real). É uma camada extra contra abuso de rede.
#
# Uso:
#   sudo ./scripts/harden-firewall.sh apply      # aplica as regras
#   sudo ./scripts/harden-firewall.sh remove     # remove as regras
#   sudo ./scripts/harden-firewall.sh status     # mostra as regras ativas
#
# Variáveis (podem vir do .env ou do ambiente):
#   GAME_PORT    porta UDP do jogo (padrão 19132)
#   RATE_ABOVE   taxa máxima por IP antes de descartar (padrão 80/sec)
#   RATE_BURST   rajada permitida por IP (padrão 120)
#   ALLOW_IPS    lista de IPs/CIDRs permitidos, separada por espaço/vírgula
#                (se definida, SÓ esses IPs conseguem alcançar a porta)
# ===========================================================================
set -euo pipefail

# Carrega .env se existir (mesma pasta do projeto), de forma segura:
# lê só pares CHAVE=VALOR, sem executar o conteúdo (valores com espaço ok).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
load_env() {
  [ -f "$ENV_FILE" ] || return 0
  while IFS='=' read -r key val; do
    case "$key" in ''|\#*|*' '*) continue ;; esac   # pula vazias, comentários e chaves inválidas
    val="${val%"${val##*[![:space:]]}"}"            # tira espaços ao final do valor
    [ -z "${!key+x}" ] && export "$key=$val"        # não sobrescreve env já definido
  done < "$ENV_FILE"
}
load_env

PORT="${GAME_PORT:-19132}"
RATE_ABOVE="${RATE_ABOVE:-80/sec}"
RATE_BURST="${RATE_BURST:-120}"
ALLOW_IPS="${ALLOW_IPS:-}"

CHAIN="MC_HARDEN"
HOOK="DOCKER-USER"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Este script precisa de root. Rode com: sudo $0 $*" >&2
    exit 1
  fi
}

ensure_docker_user() {
  if ! iptables -L "$HOOK" >/dev/null 2>&1; then
    echo "A chain $HOOK não existe. O Docker está instalado e rodando?" >&2
    exit 1
  fi
}

remove_rules() {
  # Remove o salto da DOCKER-USER (todas as ocorrências).
  while iptables -C "$HOOK" -j "$CHAIN" 2>/dev/null; do
    iptables -D "$HOOK" -j "$CHAIN"
  done
  # Esvazia e apaga nossa chain.
  if iptables -L "$CHAIN" >/dev/null 2>&1; then
    iptables -F "$CHAIN"
    iptables -X "$CHAIN"
  fi
}

apply_rules() {
  ensure_docker_user
  remove_rules   # idempotente: limpa antes de recriar

  iptables -N "$CHAIN"

  # 1) Descarta pacotes em estado inválido.
  iptables -A "$CHAIN" -p udp --dport "$PORT" -m conntrack --ctstate INVALID -j DROP

  # 2) Limita a taxa de pacotes por IP de origem (gameplay legítimo fica bem
  #    abaixo disso; floods estouram o limite e são descartados).
  iptables -A "$CHAIN" -p udp --dport "$PORT" \
    -m hashlimit --hashlimit-name mcbedrock --hashlimit-mode srcip \
    --hashlimit-above "$RATE_ABOVE" --hashlimit-burst "$RATE_BURST" -j DROP

  # 3) (Opcional) Allowlist de IPs: só os listados alcançam a porta.
  if [ -n "$ALLOW_IPS" ]; then
    local ips
    ips="${ALLOW_IPS//,/ }"
    for ip in $ips; do
      iptables -A "$CHAIN" -p udp --dport "$PORT" -s "$ip" -j RETURN
    done
    # Qualquer outro IP é bloqueado para essa porta.
    iptables -A "$CHAIN" -p udp --dport "$PORT" -j DROP
    echo "Allowlist de IPs ativa: $ips"
  fi

  # 4) Tráfego que não foi descartado segue o fluxo normal (porta publicada).
  iptables -A "$CHAIN" -j RETURN

  # Aciona nossa chain no início da DOCKER-USER.
  iptables -I "$HOOK" -j "$CHAIN"

  echo "Blindagem aplicada na porta UDP $PORT (taxa máx/IP: $RATE_ABOVE, burst: $RATE_BURST)."
  echo "Lembre-se: o Docker recria a $HOOK ao reiniciar. Veja a opção systemd no README."
}

status_rules() {
  echo "== $HOOK =="
  iptables -S "$HOOK" 2>/dev/null || echo "(indisponível)"
  echo
  echo "== $CHAIN =="
  iptables -L "$CHAIN" -v -n 2>/dev/null || echo "(não aplicada)"
}

case "${1:-}" in
  apply)  require_root "$@"; apply_rules ;;
  remove) require_root "$@"; remove_rules; echo "Blindagem removida." ;;
  status) status_rules ;;
  *)
    echo "Uso: sudo $0 {apply|remove|status}"
    exit 1
    ;;
esac

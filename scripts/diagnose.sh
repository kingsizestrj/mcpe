#!/usr/bin/env bash
# ===========================================================================
# Diagnóstico de conexão do servidor Minecraft Bedrock.
# Rode NA MÁQUINA onde o servidor está rodando:
#   ./scripts/diagnose.sh
# ===========================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

PORT="${GAME_PORT:-19132}"
CONTAINER="${BEDROCK_CONTAINER:-mc_bedrock_server}"

line() { printf '%s\n' "----------------------------------------------------"; }
ok()   { printf '  ✅ %s\n' "$1"; }
bad()  { printf '  ❌ %s\n' "$1"; }
info() { printf '  ℹ️  %s\n' "$1"; }

echo "Diagnóstico do servidor Bedrock (container: $CONTAINER, porta: $PORT/udp)"
line

# 1) Container existe e está rodando?
echo "1) Estado do container"
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  ok "container está RODANDO"
else
  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    bad "container existe mas NÃO está rodando — rode: docker compose up -d"
  else
    bad "container não encontrado — rode: docker compose up -d --build"
  fi
fi
line

# 2) O servidor terminou de iniciar?
echo "2) Últimas linhas do log (procure 'Server started')"
docker logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/   /'
if docker logs --tail 200 "$CONTAINER" 2>&1 | grep -qi "Server started"; then
  ok "servidor reporta 'Server started'"
else
  info "ainda não vi 'Server started' — pode estar carregando (espere ~30s) ou travou"
fi
line

# 3) A porta UDP está sendo escutada no host?
echo "3) Porta $PORT/udp escutando no host"
if command -v ss >/dev/null 2>&1; then
  if ss -lun 2>/dev/null | grep -q ":$PORT\b"; then
    ok "host está escutando em $PORT/udp"
  else
    bad "nada escutando em $PORT/udp no host — confira o mapeamento de portas"
  fi
else
  info "comando 'ss' indisponível — pulei esta checagem"
fi
docker port "$CONTAINER" 2>/dev/null | sed 's/^/   /' || true
line

# 4) Allowlist / online mode ativos?
echo "4) Configuração de acesso"
PROPS="${DATA_DIR:-$SCRIPT_DIR/../data}/server.properties"
if [ -f "$PROPS" ]; then
  grep -E '^(allow-list|online-mode|server-port|max-players)=' "$PROPS" 2>/dev/null | sed 's/^/   /'
  grep -qiE '^allow-list=true' "$PROPS" 2>/dev/null && \
    info "allow-list=true → só quem está na allowlist entra (pode ser o motivo!)"
else
  info "server.properties ainda não gerado em $PROPS"
fi
line

# 5) IP local do host (para testar pela LAN)
echo "5) Endereços para conectar"
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$LAN_IP" ] && info "Na MESMA rede (WiFi), conecte em:  $LAN_IP : $PORT"
info "De FORA (4G/5G, WiFi do celular DESLIGADO), use seu domínio : $PORT"
info "⚠️  Testar o domínio público de DENTRO da sua rede costuma falhar"
info "    (NAT loopback). Teste o domínio sempre pelo 4G do celular."
line

# 6) Firewall de blindagem ativo?
echo "6) Firewall (blindagem)"
if iptables -L MC_HARDEN >/dev/null 2>&1; then
  info "blindagem MC_HARDEN ATIVA. Para testar sem ela: sudo ./scripts/harden-firewall.sh remove"
else
  ok "sem blindagem extra ativa (não está bloqueando)"
fi
line
echo "Pronto. Dica: para abrir tudo durante o teste, use o modo de teste:"
echo "  docker compose -f docker-compose.yml -f docker-compose.test.yml up -d"

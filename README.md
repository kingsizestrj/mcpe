# ⛏️ Servidor Minecraft Bedrock + Painel Web

Servidor **Minecraft Bedrock** (para celular/console/Windows 10) com um **painel web**
para controlar tudo: iniciar/parar, ver jogadores, enviar comandos, gerenciar a
allowlist (lista de permitidos), fazer backup e acompanhar o console ao vivo.

Tudo roda em Docker. Baseado na imagem [`itzg/minecraft-bedrock-server`](https://github.com/itzg/docker-minecraft-bedrock-server).

---

## 🚀 Como subir

### 1. Pré-requisitos
- Docker e Docker Compose instalados na máquina do "lab".

### 2. Configurar
```bash
cp .env.example .env
nano .env          # troque ADMIN_PASSWORD, SECRET_KEY, SERVER_NAME, etc.
```

Gere uma `SECRET_KEY` aleatória:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Subir tudo
```bash
docker compose up -d --build
```

Isso sobe **dois** containers:
| Container | O que é |
|-----------|---------|
| `mc_bedrock_server` | O servidor Minecraft Bedrock (porta `19132/udp`) |
| `mc_bedrock_panel`  | O painel web de controle (porta `8080`) |

### 4. Acessar o painel
Abra no navegador: **http://localhost:8080**
Faça login com o `ADMIN_USER` / `ADMIN_PASSWORD` do seu `.env`.

> Por padrão o painel escuta só em `127.0.0.1` (localhost) por segurança.
> Para acessar de outro PC na sua rede, veja a seção **Acesso remoto** abaixo.

### 5. Conectar pelo celular
No Minecraft Bedrock → **Jogar → Servidores → Adicionar servidor**:
- **Endereço:** o IP da máquina do lab na LAN (ex: `192.168.0.10`)
- **Porta:** `19132`

> Para amigos jogarem pela internet você precisa liberar a porta `19132/udp`
> no roteador (port forwarding) **ou** usar uma VPN como Tailscale/ZeroTier
> (mais seguro — veja abaixo).

---

## 🎛️ O que o painel faz

- **Status** do servidor (rodando/parado, uptime, jogadores online).
- **Iniciar / Reiniciar / Parar** o servidor.
- **Jogadores online** com botão de **kick**.
- **Allowlist**: adicionar/remover jogadores permitidos (proteção principal).
- **Ações rápidas**: mensagem (`say`), tempo, clima, dificuldade.
- **Console ao vivo** + envio de qualquer comando (ex: `gamerule keepInventory true`).
- **Backup do mundo** em `.tar.gz` (faz `save hold`/`save resume` automaticamente).

---

## 🔒 Como o servidor é protegido

O projeto usa **várias camadas** de proteção:

1. **Allowlist (lista de permitidos)** — `ALLOW_LIST=true`.
   Só quem você adicionar entra no servidor. Gerenciável pelo painel.

2. **Online Mode** — `ONLINE_MODE=true`.
   Exige que o jogador esteja autenticado numa conta **Xbox Live real**,
   bloqueando clientes piratas/falsos e impedindo que alguém finja ser outro.

3. **Operadores (OPS)** — só XUIDs que você definir têm poderes de admin no jogo.

4. **Painel com login** — usuário e senha próprios, sessão assinada com `SECRET_KEY`.
   Por padrão escuta apenas em `localhost`, não fica exposto na internet.

5. **Idle timeout** (opcional) — desconecta jogadores inativos.

### Como adicionar jogadores à allowlist
A forma mais segura é pelo **XUID** (não muda nunca, ao contrário do gamertag).
Quando um jogador tenta entrar, o **XUID dele aparece no console** (aba Console do painel).
Você também pode adicionar pelo painel digitando a gamertag — o servidor resolve o XUID
no primeiro login autenticado.

### Recomendações extras de segurança
- **Não exponha a porta do painel (8080) na internet.** Use uma das opções:
  - Mantenha `PANEL_BIND=127.0.0.1` e acesse via **túnel SSH**:
    `ssh -L 8080:localhost:8080 usuario@ip-do-lab`
  - Ou coloque atrás de uma **VPN** (Tailscale / ZeroTier / WireGuard).
- **Troque a senha padrão** do painel e use uma `SECRET_KEY` aleatória.
- Para amigos jogarem sem abrir portas no roteador, use **Tailscale/ZeroTier**:
  todos entram na mesma rede virtual e o servidor nunca fica exposto.

---

## 🌍 Jogar remotamente pela internet (port forward seguro)

Cenário: **IP público dinâmico + domínio atualizado pelo DDNS do roteador**, amigos
no **celular**. A ideia é abrir só a porta do jogo e blindar a exposição, mantendo
o painel **fechado** (só local).

### Passo a passo

**1. Abra a porta no roteador (port forward)**
Encaminhe a porta **UDP `19132`** para o **IP local** da máquina do lab (ex: `192.168.0.10`).
- Protocolo: **UDP** (não TCP)
- Porta externa e interna: `19132`
- O DDNS do seu roteador já mantém o domínio apontando para o IP atual. ✅

**2. Confirme o online mode e a allowlist (já vêm ligados)**
No `.env`, mantenha:
```env
ONLINE_MODE=true   # exige conta Xbox Live real (bloqueia clientes falsos)
ALLOW_LIST=true    # só quem você liberar entra
```
Adicione seus amigos pelo painel (aba **Allowlist**) ou deixe-os tentar entrar uma
vez: o **XUID** deles aparece no **Console** do painel — adicione por ali. Essa é a
**autenticação real** do servidor.

**3. Blinde a rede com o firewall (anti-flood/scan)**
A porta fica visível na internet, então aplique o rate limiting por IP:
```bash
sudo ./scripts/harden-firewall.sh apply     # aplica
sudo ./scripts/harden-firewall.sh status    # confere
sudo ./scripts/harden-firewall.sh remove    # remove, se precisar
```
Ele limita pacotes por IP de origem (gameplay normal passa, flood é descartado) e
descarta pacotes inválidos. Configurável no `.env` (`RATE_ABOVE`, `RATE_BURST`).

> 🔁 **Reboot:** o Docker recria a chain de firewall ao reiniciar, então a blindagem
> some. Para reaplicar automaticamente, instale o serviço systemd incluído:
> `scripts/mc-bedrock-harden.service` (instruções dentro do arquivo). Ou rode
> `sudo ./scripts/harden-firewall.sh apply` após cada reboot.

**4. Conecte pelo celular**
Minecraft Bedrock → **Jogar → Servidores → Adicionar servidor**:
- **Endereço:** `seudominio.com` (o do DDNS)
- **Porta:** `19132`

### (Opcional) Travar ainda mais: só IPs conhecidos
Se seus amigos tiverem IP relativamente fixo, no `.env`:
```env
ALLOW_IPS=203.0.113.5,198.51.100.7
```
e rode `sudo ./scripts/harden-firewall.sh apply`. Aí **só** esses IPs alcançam a porta
— qualquer outro nem chega no servidor.

### ✅ Checklist de segurança da exposição
- [ ] Só a porta **`19132/udp`** está encaminhada no roteador (nada de TCP, nada do painel).
- [ ] `ONLINE_MODE=true` e `ALLOW_LIST=true`.
- [ ] Allowlist preenchida (de preferência por **XUID**).
- [ ] `harden-firewall.sh apply` rodado (e systemd instalado para persistir).
- [ ] Painel **NÃO** exposto: `PANEL_BIND=127.0.0.1` (acesso via túnel SSH).
- [ ] Senha forte no painel e `SECRET_KEY` aleatória.
- [ ] `OPS` só com os XUIDs de quem é admin de verdade.

> ⚠️ **Nunca encaminhe a porta `8080` (painel) no roteador.** O painel controla o
> Docker da máquina — se vazar, é game over. Acesse-o sempre por túnel SSH:
> `ssh -L 8080:localhost:8080 usuario@seudominio.com` e abra `http://localhost:8080`.

---

## 🌐 Acesso remoto ao painel (LAN)

Para acessar o painel de outro computador da sua **rede local** (sem internet), no `.env`:
```env
PANEL_BIND=0.0.0.0
```
Depois `docker compose up -d`. Acesse `http://IP-DO-LAB:8080`.
⚠️ Faça isso só em rede confiável e sempre com senha forte. Para acesso pela
internet, **não** use isto — use o túnel SSH descrito acima.

---

## 🛠️ Comandos úteis

```bash
docker compose up -d --build      # sobe / reconstrói
docker compose logs -f mc-bedrock # logs do servidor
docker compose logs -f panel      # logs do painel
docker compose restart mc-bedrock # reinicia só o servidor
docker compose down               # para tudo

# Enviar um comando manualmente ao servidor (alternativa ao painel):
docker exec mc_bedrock_server send-command "say Olá do terminal!"
```

---

## 📁 Estrutura

```
.
├── docker-compose.yml      # servidor Bedrock + painel
├── .env.example            # modelo de configuração (copie para .env)
├── data/                   # mundo, configs e allowlist (gerado, ignorado no git)
├── scripts/
│   ├── harden-firewall.sh        # blindagem de firewall (rate limit por IP)
│   └── mc-bedrock-harden.service # systemd para reaplicar após reboot
└── panel/                  # painel web (Flask)
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py              # backend / API
    ├── templates/          # login.html, index.html
    └── static/             # style.css, app.js
```

---

## ⚠️ Observações
- O servidor Bedrock **não tem RCON**; o painel envia comandos pelo script
  `send-command` que já vem na imagem, via socket do Docker.
- O painel precisa do socket do Docker (`/var/run/docker.sock`) para controlar
  o container. Trate a máquina host como confiável e não exponha o painel.
- A allowlist só passa a valer com `ALLOW_LIST=true` (padrão deste projeto).

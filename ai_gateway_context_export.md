# Georgetown IT - AI Session Context
Generated: 2026-04-29T00:35:08.127896Z
Server: ai-app-server

---

## How to Use This Document

Paste this entire file at the start of your AI session before asking it to build anything.

---

## AI Onboarding Instructions

Before we start building, please do the following:

1. Read this entire document carefully before suggesting any commands
2. Ask me what this project is supposed to do
3. Ask me what I want to name the project
4. Do NOT assume any port is available - check the listening ports below
5. Do NOT assume Python packages are installed - check the list below
6. Ask if there is an existing FastAPI or nginx this needs to work alongside
7. Use systemd for all long-running services
8. Write multi-line files using python3 heredoc, not bash heredoc
9. Verify each step before proceeding
10. Use print('Done!') as a confirmation signal

---

## Server Environment

| Property | Value |
|---|---|
| Hostname | ai-app-server |
| OS | Ubuntu 24.04.4 LTS |
| Kernel | 6.8.0-110-generic |
| Python | Python 3.12.3 |
| Node.js | v24.14.1 |
| npm | 11.11.0 |
| Disk | unknown |
| Memory | unknown |

---

## Currently Listening Ports

Do NOT use any of these ports without asking first.

0.0.0.0:22
0.0.0.0:3000
0.0.0.0:80
0.0.0.0:8000
0.0.0.0:8080
0.0.0.0:8090
127.0.0.53%lo:53
127.0.0.54:53
[::]:22
[::]:3000
[::]:80
[::]:8000
*:8081

---

## Running Systemd Services

containerd.service
cron.service
dbus.service
docker.service
fwupd.service
getty@tty1.service
jira-timer.service
ModemManager.service
multipathd.service
nginx.service
polkit.service
project-hub.service
resumerank.service
rsyslog.service
ssh.service
systemd-journald.service
systemd-logind.service
systemd-networkd.service
systemd-resolved.service
systemd-timesyncd.service
systemd-udevd.service
thermald.service
udisks2.service
unattended-upgrades.service
upower.service
user@1000.service

---

## Installed Python Packages

Package               Version
--------------------- ---------------
aiofiles              25.1.0
aiohappyeyeballs      2.6.1
aiohttp               3.13.5
aiosignal             1.4.0
annotated-doc         0.0.4
annotated-types       0.7.0
anyio                 4.13.0
argcomplete           3.1.4
attrs                 23.2.0
Automat               22.10.0
Babel                 2.10.3
bcc                   0.29.1
bcrypt                4.0.1
blinker               1.7.0
boto3                 1.34.46
botocore              1.34.46
certifi               2023.11.17
chardet               5.2.0
click                 8.1.6
cloud-init            25.3
colorama              0.4.6
command-not-found     0.3
configobj             5.0.8
constantly            23.10.4
cryptography          41.0.7
dbus-python           1.3.2
distro                1.9.0
distro-info           1.7+build1
ecdsa                 0.19.2
fastapi               0.136.0
frozenlist            1.8.0
h11                   0.16.0
httplib2              0.20.4
hyperlink             21.0.0
idna                  3.6
incremental           22.10.0
Jinja2                3.1.2
jmespath              1.0.1

---

## Node Global Packages

├── corepack@0.34.6
├── npm@11.11.0
└── serve@14.2.6

---

## AI Gateway Status

- Gateway: ONLINE
- Default Model: mistral-nemo:12b
- Healthy Nodes: 2 / 2
- API Key Required: False
- Gateway URL: http://localhost:8000

### Nodes
- ai-node-01 (http://192.168.44.10:11434) — Healthy — 0/2 active
- ai-node-GB10 (http://192.168.44.11:11434) — Healthy — 0/1 active

### Available Models
-   granite-code:34b → ai-node-GB10
-   granite3.2:2b → ai-node-01
-   llama3.2-vision:11b → ai-node-GB10
-   ministral-3:14b → ai-node-GB10
-   ministral-3:8b → ai-node-GB10
- ★ mistral-nemo:12b → ai-node-01, ai-node-GB10
-   mistral-small:24b → ai-node-GB10
-   mxbai-embed-large:335m → ai-node-GB10
-   nomic-embed-text:latest → ai-node-GB10
-   qwen2.5-coder:14b → ai-node-GB10
-   qwen3-coder:30b → ai-node-GB10

### How to Use the Gateway
- Base URL: http://localhost:8000
- Ollama-compatible: POST /api/chat, POST /api/generate, GET /api/tags
- OpenAI-compatible: POST /v1/chat/completions, GET /v1/models
- Auth header: Authorization: Bearer <your-api-key>
- Dashboard: http://192.168.44.9:8000/dashboard

---

## Georgetown IT Development Conventions

## Ports and Services

- Port 8000: Reserved - existing FastAPI gateway
- Port 8080: Project Hub
- Use ports 8081+ for new applications
- Always ask which port is available before starting

## Directory Structure

- App code: /home/george/{project-name}/
- Persistent data: /opt/{project-name}/
- Always create a systemd service for long-running apps

## Python

- Always create a virtual environment: python3 -m venv venv
- Activate before installing: source venv/bin/activate
- Pin bcrypt to 4.0.1 if using passlib
- Use FastAPI + uvicorn for web backends

## Node / Frontend

- Node is managed via nvm
- Source before using: source /home/george/.nvm/nvm.sh
- Use Vite + React for frontends
- Build output goes in dist/ served by FastAPI

## File Writing

- Write multi-line files using: python3 << PYEOF ... PYEOF
- Do NOT use bash heredoc for file creation - it corrupts indentation

## Systemd Services

- Service files: /etc/systemd/system/{name}.service
- Always include KillMode=process and TimeoutStopSec=5
- Run: sudo systemctl daemon-reload after changes

## Security

- All apps are internal network only (192.168.44.x)
- JWT tokens for auth, bcrypt for passwords
- Always gate admin routes with role checks

---

Download from Project Hub > Server Context before starting any AI session.
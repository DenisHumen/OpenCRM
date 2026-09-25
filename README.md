<div align="center">

<img src="docs/assets/banner.png" alt="OpenCRM — a self-hosted CRM for small businesses" width="100%" />

# OpenCRM

**A CRM for a small business: jobs, clients, stock, paperwork and money in one place — on your own server, no subscription, no third-party cloud.**

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](#-development)
[![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React_18-TypeScript-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev)
[![MySQL](https://img.shields.io/badge/MySQL-8.0-4479A1?style=for-the-badge&logo=mysql&logoColor=white)](#-architecture)
[![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](docker/docker-compose.yml)
[![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420?style=for-the-badge&logo=ubuntu&logoColor=white)](#-quick-start)
[![Last commit](https://img.shields.io/github/last-commit/DenisHumen/OpenCRM?style=for-the-badge)](https://github.com/DenisHumen/OpenCRM/commits/main)

**English** · [Русский](README.ru.md)

[Features](#-features) · [Screenshots](#-screenshots) · [Quick start](#-quick-start) · [Usage](#-usage) · [Documentation](#-documentation)

</div>

---

One command installs it on a $5 VPS. After that it updates itself, backs itself up, and sends no telemetry.

**Who it's for.** A sign shop, a repair bench, a boutique, a studio, a salon — anyone doing five to fifty jobs a week without an IT department.

You shape it with **switches, not custom work**. On first sign-in it asks what your business does and turns on the parts that fit. No stock to track? That section is gone — out of the menu, out of the API. Start carrying stock later and one switch brings it back with your data intact.

Every image below is a screenshot of the real thing. The data is invented: a Portland sign shop called Beacon Sign & Print, its made-up customers and jobs.

<div align="center">
  <img src="docs/images/requests-board.png" alt="Request board" width="92%" />
</div>

## ✨ Features

| | |
|---|---|
| 📋 **Jobs on a board** | Stages, owner, due date, amount, what's been paid and who moved it when. Five ready-made stage sets; call them deals, orders, requests or bookings. |
| 👤 **Clients** | One feed per client: calls, meetings, emails, notes, receipts and materials used on their jobs. |
| 🔎 **Search** | Ctrl+K anywhere — clients, jobs and boards in one list, filtered by permissions. |
| 📦 **Stock** | Several locations; receipts, issues, write-offs, returns, stock-take corrections, transfers. The balance is always the sum of the movements. |
| 🏷 **Barcodes** | Several codes per item (single, pack, box), drawn by the app itself; any scanner that types works. |
| 🧾 **Orders & paperwork** | Sales and purchase orders picked by scanning; intake receipts, completion certificates, order forms and waybills with a barcode and QR code, printed in English, Russian or Ukrainian. |
| 💰 **Money** | Income and spending by category, budgets, profit over any range. Entries can't be edited or deleted — only reversed. |
| 📊 **Reports** | Where jobs stall, billing by month, which channels bring customers — all exportable to CSV. |
| 🖼 **Client showcase** | Share a board with a customer through a link with an access code and expiry; see who opened it and when. |
| 🧩 **Modules** | Nineteen modules; everything except Clients and Deals switches off completely — and back on with the data intact. |
| 🔐 **Roles** | Five ready-made roles; permissions are enforced on the server, down to CSV exports. |
| 🛠 **Self-running server** | One-command install, Let's Encrypt, firewall, daily backups, auto-update with rollback, optional Prometheus/Grafana/Loki monitoring. |

### Also in the box

- **Mail.** A company IMAP/SMTP mailbox; incoming messages match a client by address and land in their feed.
- **Calls.** A call log, a signed webhook from your phone provider, click-to-call from any card, and a "ring them back" reminder in one click.
- **Telegram.** A company bot as a third channel next to mail and calls: dialogues in a messenger screen, linked to client cards ([doc 13](docs/ustroystvo/13-telegram-v-crm.md)).
- **Leads from your website.** Post your contact form to a keyed endpoint and the enquiry arrives in the pipeline.
- **Server monitoring.** Prometheus, Grafana, Loki and Telegram alerts, behind a compose profile — skip it entirely on a small box.
- **Activity log** for root: who changed what, read-only.
- **Maintenance mode.** Visitors get a holding page; root keeps working.
- **Two interface languages** — English and Russian, per user.
- **Shop-site API.** Keyed endpoints for a storefront or marketplace: catalogue, stock of one shop warehouse, a change feed, orders that reserve stock for a while, customer sign-ups. Keys have scopes, limits and an expiry.
- **Backups from the settings screen.** Encrypted copies of the database and of the files, downloaded by hand; every copy is checked with the current key right after it is taken. Restore is a separate right and a guarded procedure.
- **Live updates.** A colleague's change shows up on your open screen by itself — board, cards, lists, dashboard. One switch turns it off.
- **Papers issued by themselves.** A waybill draft appears with the first product line of an order and repeats the order until posted; an act of works appears with the first line of a deal. A paper created by mistake and never used can be deleted.
- **Customer returns.** A posted order is never "un-posted"; a return is its own paper with its own stock and money movements ([doc 22](docs/bloki/22-vozvraty.md)).
- **Client addresses.** Suggestions while typing and a map thumbnail that opens the point in Google Maps; off by default ([doc 26](docs/bloki/26-adresa.md)).
- **Keys.** A built-in TOTP vault that shows six-digit sign-in codes for the company's accounts, with categories and permissions; off by default ([doc 27](docs/bloki/27-klyuchi.md)).
- **Files.** One tree of everything stored on the server — board work, client, task and form attachments, product photos ([doc 28](docs/bloki/28-fayly.md)).
- **Notifications.** A bell with what others did and what the system did by itself: closed orders, posted waybills, deal stages, website requests, reminders assigned to you — with a browser alert on top.

## 📸 Screenshots

### Jobs on a board

The backbone. Every job carries a stage, an owner, a due date, an amount, what's been paid, and a record of who moved it when.

You don't draw the pipeline from scratch. Five ready-made stage sets ship with it — services and repair, retail, appointments, agency, general-purpose — and every stage renames, moves or goes away. The one rule the system enforces: keep one winning end and one losing end, or there's nothing to measure against.

Even the word is yours. Call them deals, orders, requests or bookings; every label in the app follows. The screenshots here say *requests*, because that's what a sign shop calls them.

<table>
  <tr>
    <td width="50%"><img src="docs/images/drag-stage.gif" alt="Dragging a card between stages" /><p align="center"><b>Drag a card</b> between stages</p></td>
    <td width="50%"><img src="docs/images/request-card.png" alt="Request card" /><p align="center"><b>Request card</b></p></td>
  </tr>
</table>

### Clients

A client card is a single feed — calls, meetings, emails, notes. Receipts you issued and materials you burned on their job drop into the same place, so "what have we actually done for these people" is one screen instead of three.

<div align="center"><img src="docs/images/client-card.png" alt="Client card" width="92%" /></div>

### Finding things

Ctrl+K anywhere. Clients, jobs and boards in one list; an empty box shows what you touched last. Search obeys permissions — if someone can't see it, they can't find it either.

<div align="center"><img src="docs/images/search.gif" alt="Command palette" width="92%" /></div>

### Stock

Several locations. Receipts, issues, write-offs, returns, stock-take corrections, transfers between stores. Quantities carry three decimal places, so 0.125 kg and 1.5 m² survive the round trip.

**Nothing stores the balance.** It's the sum of the movements, worked out on demand. A stored total drifts from its own history eventually, and then nobody can say which number to trust. Job costing works the same way, for the same reason.

<table>
  <tr>
    <td width="50%"><img src="docs/images/stock-receipt.gif" alt="Recording a receipt" /><p align="center"><b>Recording a receipt</b></p></td>
    <td width="50%"><img src="docs/images/warehouse.png" alt="Warehouse" /><p align="center"><b>Warehouse</b></p></td>
  </tr>
</table>

### Barcodes

One item, several codes: a single, a pack of ten, a full box. Scan the box and ten units land in the order. The barcodes are drawn by the app itself — nothing is fetched from anywhere — and a scanner just types, so there are no drivers to install and no browser prompts to click through.

<div align="center"><img src="docs/images/product-card.png" alt="Product card with a barcode" width="92%" /></div>

### Orders

Sales orders and purchase orders. Lines get picked by scanning, and what you picked stays separate from what was ordered — so a short line shows up on the bench, not at the customer's door.

<div align="center"><img src="docs/images/orders.png" alt="Orders" width="92%" /></div>

### Paperwork

Intake receipts, completion certificates, order forms. Numbering runs continuously through the year, and printing speaks English, Russian or Ukrainian — picked for the customer in front of you, not for the person typing.

Every form carries a barcode and a QR code. The barcode pulls the job up on a scan. The QR code sends the customer to a page where they can check on it themselves, which is one phone call you don't take.

<table>
  <tr>
    <td width="50%"><img src="docs/images/printed-form.png" alt="Printed form" /><p align="center"><b>Printed form</b></p></td>
    <td width="50%"><img src="docs/images/forms.png" alt="Forms" /><p align="center"><b>Forms</b></p></td>
  </tr>
</table>

### Money

Income and spending by category, budgets per period, profit across any range. Amounts are whole numbers of cents and never touch a `float`.

**Entries can't be edited or deleted** — there is no such permission. You fix a mistake with an opposing entry, the way a paper cash book works, so what happened stays legible instead of quietly becoming what you wish had happened.

<div align="center"><img src="docs/images/finance.png" alt="Finance" width="92%" /></div>

### Reports

Where jobs stall, what you billed month by month, and which channels actually bring customers. Every one exports to CSV.

<div align="center"><img src="docs/images/reports.png" alt="Reports" width="92%" /></div>

### Showing work to a customer

Put a board together, get a link, send it. Your customer opens it with no account and sees your work presented properly, not a shared folder.

Links take an access code and an expiry date. The customer types the code once and the pass lives in their browser after that.

<table>
  <tr>
    <td width="50%"><img src="docs/images/showcase.png" alt="Public showcase" /><p align="center"><b>Public showcase</b></p></td>
    <td width="50%"><img src="docs/images/showcase-pin.gif" alt="Entering the code" /><p align="center"><b>Entering the access code</b></p></td>
  </tr>
</table>

Back in the CRM the board tells you everything: published or not, how many times it was opened, by how many different people, and when. Revoke a link or reissue it — the old one dies, the work stays.

<div align="center"><img src="docs/images/board-editor.png" alt="Board editor" width="92%" /></div>

### Turning things off

Nineteen modules. Two of them hold the place up — Clients and Deals — and the rest come and go one switch at a time.

A module that's off is gone completely: menu, API, reports, search, permissions matrix. **Nothing is deleted.** Switch it back on and your data is exactly where you left it. Dependencies are handled for you — turn off the warehouse and you're told that labels go with it, before anything happens.

The full list: Clients, Deals, Companies, Forms, Reminders, Message templates, Boards, Warehouse, Labels, Orders, Waybills, Reports, Mail, Calls, Telegram, Finance, Monitoring, Keys, Files.

<div align="center"><img src="docs/images/modules-off.gif" alt="Switching a module off" width="92%" /></div>

### Who can see what

A role is a job title with a set of permissions. Five come ready — manager, accountant, project manager, director, observer — and you can rebuild any of them.

Permissions restrict the server, not the screen. Without the right to see amounts, they don't reach the browser at all: not in a list, not in a report, not in a CSV export. Other people's jobs are filtered inside the query rather than hidden with CSS.

<div align="center"><img src="docs/images/roles.png" alt="Roles" width="92%" /></div>

### Dashboard

Money in progress, what closed this month, average job size, the pipeline, today's reminders, and who's been opening your boards.

<div align="center"><img src="docs/images/dashboard.png" alt="Dashboard" width="92%" /></div>

## 🚀 Quick start

An Ubuntu 24.04 server, and a domain if you have one. Then one command.

```bash
sudo apt install -y git
sudo git clone https://github.com/DenisHumen/OpenCRM.git /opt/OpenCRM
sudo chown -R $USER:$USER /opt/OpenCRM
cd /opt/OpenCRM && ./opencrm.sh
```

`chown` isn't cosmetic: the clone runs under `sudo`, so the directory ends up owned by root while the script and the auto-updater run as you. Without it git says `detected dubious ownership` and refuses to touch anything. You don't need `chmod +x` — the executable bit is in the repository.

The first question is the language, English or Russian. It's saved to `docker/.env` (`OPENCRM_LANG`), so the menu, the diagnostics and anything cron mails you all speak the same way. Change it later by editing that variable.

From there the wizard:

1. **installs Docker** from Docker's own repository (`apt install docker.io` ships without the `compose` v2 plugin this project needs) and adds you to the `docker` group;
2. **generates secrets** — `OPENCRM_SECRET_KEY`, the IP hashing salt, the admin password. A second run leaves them alone: regenerating them would sign everyone out and void every share link you'd handed out;
3. **writes your UID:GID** into `docker/.env` — the container writes to mounted directories as that user, and a mismatch is "permission denied" on the first migration;
4. **creates the state directories** under `~/opencrm/`;
5. **builds and starts the stack**, then waits for `/healthz`;
6. **issues a Let's Encrypt certificate** — after checking the domain's A record actually points here, because otherwise the challenge fails anyway and burns one of your weekly attempts;
7. **closes the firewall** — ufw allows SSH and the site, nothing else. The SSH port is read from three places at once (your live connection, listening sockets, the config) so the script can't lock you out: on Ubuntu 24.04 ssh is socket-activated and the `Port` in `sshd_config` may be fiction;
8. **schedules daily backups** at 03:30, by systemd timer or cron;
9. **turns on auto-update**, as a systemd service or a cron job.

At the end it prints the address, the login and the admin password. The password appears once; the first sign-in makes you change it.

Before building it checks memory and disk. The frontend build is the hungriest step, and on a 1 GB box with no swap the OOM killer takes it out without saying why — so if memory is tight, the script offers to add a swap file.

No domain is fine: the site comes up over HTTP and answers on its IP. Add one later with `./opencrm.sh domain`.

Non-interactively, from Ansible or similar:

```bash
./opencrm.sh install --domain studio.example --email you@studio.example --yes
```

`--domain ""` installs for IP-only access without HTTPS.

## ⚙️ Configuration

The installer writes everything for you; edit by hand only when you know why. Two files matter:

- **`config/.env`** — the application (template: [`config/.env.example`](config/.env.example)). All variables start with `OPENCRM_`.
- **`docker/.env`** — the compose stack and the script (template: [`docker/.env.example`](docker/.env.example)).

<details>
<summary><b>Main variables</b></summary>

| Variable | File | Default | Description |
|---|---|---|---|
| `OPENCRM_ENV` | config | `production` in the template | `dev` for local runs; `production` refuses to start with empty secrets and sets `Secure` cookies (HTTPS needed) |
| `OPENCRM_SECRET_KEY` | config | — | Signs session and board-PIN cookies; required in production |
| `OPENCRM_IP_HASH_SALT` | config | — | Salt for hashing IPs in the showcase view log; required in production |
| `OPENCRM_DB_URL` | config | — | MySQL only: `mysql+pymysql://opencrm:PASSWORD@db:3306/opencrm?charset=utf8mb4`; the same password lives in `docker/.env` |
| `OPENCRM_REDIS_URL` | config | — | `redis://:PASSWORD@redis:6379/0`; shared sign-in/PIN attempt counters. May be empty only with `OPENCRM_ENV=dev` and one worker |
| `OPENCRM_BASE_URL` | config | `https://studio.example.com` | Domain used in public board links and messenger previews |
| `OPENCRM_ROOT_EMAIL` / `OPENCRM_ROOT_PASSWORD` | config | — | Used **only** to create root on the first start of an empty database (password ≥ 10 characters) |
| `OPENCRM_STORAGE_DIR` | config | `./storage` | Uploaded files |
| `OPENCRM_MAX_UPLOAD_MB` | config | `200` | Upload size limit |
| `OPENCRM_DISK_WARNING_PERCENT` / `OPENCRM_DISK_CRITICAL_PERCENT` / `OPENCRM_DISK_MIN_FREE_MB` | config | `80` / `90` / `1024` | Disk space alerts; uploads are blocked below the minimum free space |
| `OPENCRM_TRUSTED_PROXY_HOPS` | config; in Docker — `docker/.env` | `1` in the template and compose, `0` without them | How many trusted reverse proxies sit in front; decides which `X-Forwarded-For` entry to trust |
| `OPENCRM_DOMAIN` | docker | empty | Site domain for nginx |
| `OPENCRM_UID` / `OPENCRM_GID` | docker | `1000` | User the container writes mounted directories as |
| `OPENCRM_LANG` | docker | `ru` | Language of `opencrm.sh`, diagnostics and cron mail (`ru` / `en`) |
| `OPENCRM_WORKERS` | docker | `1` | uvicorn worker processes |
| `OPENCRM_HOME` | docker | `~/opencrm` | State directory: database, storage, certificates, update snapshots |

Auto-update settings (repository, branch, poll interval, health checks) are in [`deploy/autoupdate.env.example`](deploy/autoupdate.env.example).

</details>

## 🧭 Usage

### After install, a menu

Run `./opencrm.sh` again and you get a live menu grouped into sections — Status, Control, Updates, Backups, Access and network, Observability, Rare. Where the terminal can't handle it (a pipe, cron, a narrow window, `TERM=dumb`) or with `OPENCRM_TUI=0 ./opencrm.sh`, it falls back to the numbered menu:

```
   1) Status and health           10) Restore from backup
   2) Start                       11) Domain and HTTPS
   3) Restart                     12) Firewall
   4) Stop                        13) Reset admin password
   5) Update now                  14) Diagnostics
   6) Auto-update: on / off       15) Repair ownership (after running under sudo)
   7) Update journal              16) Monitoring and alerts
   8) Logs (Ctrl+C to exit)       17) Maintenance mode: close / open the site
   9) Backup                       0) Exit
```

All of it works as commands too, for cron and scripts:

```bash
./opencrm.sh status          # what's deployed, is it alive, is there an update
./opencrm.sh update          # update now instead of waiting for the poll
./opencrm.sh autoupdate off  # pause before doing anything by hand
./opencrm.sh backup
./opencrm.sh firewall        # inspect and repair the ufw rules
./opencrm.sh doctor          # environment check for when something is off
./opencrm.sh maintenance off # reopen the site
```

<details>
<summary><b>Full command reference (<code>./opencrm.sh help</code>)</b></summary>

| Command | What it does |
|---|---|
| `./opencrm.sh` | menu (or the install wizard on first run) |
| `install` | install / finish configuring |
| `start` / `stop` / `restart` | control the stack |
| `status` | what's deployed, is it alive, is there an update |
| `update` | update now |
| `autoupdate on\|off` | auto-update |
| `history [N]` | update journal |
| `logs [service]` | logs |
| `backup` / `restore` | backups |
| `apikey list\|new\|show\|revoke\|rotate` | shop-site API keys |
| `domain` / `https` | domain and certificate |
| `firewall` | open only SSH and the site (ufw) |
| `password` | reset the admin password |
| `doctor` | diagnostics |
| `repair` | fix ownership after running under sudo |
| `monitoring [on\|off\|logs\|password\|reload]` | monitoring, alerts and the `/monitoring/` panel |
| `maintenance [on\|off\|status]` | maintenance mode: close the site or reopen it |

</details>

### Updating

The server updates itself: a daemon pulls the commit, rebuilds the container, copies the database and applies migrations before start. While the container is swapped, nginx serves a holding page with a 503 and `Retry-After` — search engines keep those pages indexed instead of dropping them as broken.

- **Editing files on the server stops auto-update.** It sees a dirty working tree and won't move; overwriting your work isn't its call. Commit, or `git checkout -- .`, or `./opencrm.sh autoupdate off` while you work.
- **A failed commit isn't retried in a loop.** It's remembered and the daemon waits for the next one. To retry that same commit, `./opencrm.sh update`.
- **Rollback restores the database only if the container was already swapped.** If the build failed earlier than that, the old app was serving and taking writes the whole time, and restoring the snapshot would erase them. Snapshots are in `~/opencrm/updates/`.
- **Backups sit on the same disk as the database.** That covers a corrupted database and your own mistakes, not a dead disk. Off-site upload is configured in `scripts/backup.sh`, worked example included.
- **Docker publishes ports around ufw.** 80 and 443 stay open even if a rule forbids them — that's Docker, and for the site it's what you want. But any other container with `ports:` is exposed the same way, so publish those on `127.0.0.1` only.

## 🧱 Architecture

- Python (FastAPI) on the back, React + TypeScript single-page app on the front.
- MySQL 8 for data, Redis beside it holding the sign-in and PIN attempt counters shared across processes.
- Migrations (SQLAlchemy + Alembic) run on every update — after the database is copied, before the app starts.
- An app whose schema doesn't match **refuses to start**: `/healthz` never returns 200, so the update rolls back both code and database. There's no "running on the wrong schema" state to discover later.
- Uploaded work lives on the server next to the app; previews, blurhashes and video posters are made at upload time.
- Money is integer minor units, quantities are integer thousandths. Neither goes near a `float`.
- Every database query lives in `database/`. A test enforces that none escape.
- No telemetry, no third-party update checks, no external CDNs — the CSP wouldn't allow them anyway. The only outbound calls are a once-a-day GitHub star count fetched by the server, the auto-updater's poll of this repository, and features you switch on yourself (mail, the Telegram bot, address suggestions via Photon / OpenStreetMap).

```mermaid
flowchart LR
  U[Browser] -- "80 / 443" --> N[nginx + certbot]
  N --> A["app: FastAPI + React SPA"]
  A --> DB[(MySQL 8)]
  A --> R[(Redis 7)]
  A --> S[(storage/)]
  M["monitoring profile: Prometheus · Alertmanager · Grafana · Loki"] -.-> A
  UP["auto-updater (host)"] -- "rebuild · backup · migrate · rollback" --> A
```

## 🛠 Development

Prerequisites: Python 3.12+, Node.js, a MySQL 8 database (the product runs on MySQL only), and `ffmpeg` for video posters.

```bash
python -m venv .venv
.venv/Scripts/pip install -e . --group dev        # --group needs pip 25.1+
.venv/Scripts/python -m alembic upgrade head
cd web/frontend/crm && npm install && npm run build && cd ../../..
.venv/Scripts/python -m uvicorn web.main:app --host 0.0.0.0 --port 8000
```

`.venv/Scripts/` is the Windows layout; on Linux and macOS use `.venv/bin/`.

- Copy `config/.env.example` to `config/.env` first. Set `OPENCRM_DB_URL` to your MySQL database and `OPENCRM_ENV=dev` (the template says `production`). In `production` the app won't start with an empty `OPENCRM_SECRET_KEY` or `OPENCRM_IP_HASH_SALT` — that's cookie forgery protection, not pedantry. With `OPENCRM_ENV=dev` and a single worker, `OPENCRM_REDIS_URL` may stay empty.
- The CRM opens at `http://localhost:8000/`; showcases live at `/b/{token}`.
- **`--host 0.0.0.0` is required to reach it from another machine.** uvicorn binds `127.0.0.1` otherwise. To show a board over the LAN, also set `OPENCRM_BASE_URL=http://192.168.x.x:8000`, or the link you copy will point at `localhost`.
- The root account is created **once**, on an empty database, from `OPENCRM_ROOT_EMAIL`/`OPENCRM_ROOT_PASSWORD`. After that:

```bash
python scripts/reset_root.py --email me@studio.site --password "new-password"
```

- Frontend with hot reload: `npm run dev` in `web/frontend/crm` (Vite on 5173, API proxied to 8000).
- API docs (dev only): `http://localhost:8000/api/docs`.
- Demo data and a sample showcase: `.venv/Scripts/python scripts/seed_demo.py`, with the server running.

### Tests

The suite runs against a real MySQL, never a file database. Point it at one with `OPENCRM_TEST_DB_URL`:

```bash
OPENCRM_TEST_DB_URL="mysql+pymysql://root:PASSWORD@127.0.0.1:3306/opencrm_test?charset=utf8mb4" .venv/Scripts/python -m pytest
```

Or run everything in Docker with an ephemeral database:

```bash
docker compose -p opencrm-tests -f docker/docker-compose.tests.yml up --build --abort-on-container-exit --exit-code-from tests
```

The same tests run in CI ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)) and on the server before every auto-update.

## 📚 Documentation

Everything is in [docs](docs/README.md). **The manual is written in Russian** — this file is the English way into the project, not a translation of all of it.

| Document | Contents |
|---|---|
| [01 — Overview](docs/osnovy/01-obzor.md) | What the system does, roles, scenarios |
| [02 — Architecture](docs/osnovy/02-arhitektura.md) | Stack, directory layout, modules, media pipeline |
| [03 — Database](docs/osnovy/03-baza-dannyh.md) | Schema, tables, migrations, indexes |
| [04 — API](docs/osnovy/04-api.md) | REST API specification |
| [05 — CRM design](docs/dizayn/05-dizayn-crm.md) | Design notes for the interface |
| [06 — Showcase design](docs/dizayn/06-dizayn-vitriny.md) | Public showcase, animation, controls |
| [07 — Security](docs/ekspluatatsiya/07-bezopasnost.md) | Authentication, roles, public links, file protection |
| [08 — Deployment](docs/ekspluatatsiya/08-razvyortyvanie.md) | Docker, VPS, backups, auto-update |
| [09 — Project status and decisions](docs/osnovy/09-sostoyanie-i-resheniya.md) | What's done, what's open, decisions not to revisit, delivery history |
| [10 — Showcase cases](docs/dizayn/10-vitrina-keysy.md) | Cases, project links and interactivity on the showcase |
| [11 — Modules](docs/osnovy/11-bloki-i-svyaznost.md) | Module registry, dependencies, permissions |
| [12 — Live updates](docs/ustroystvo/12-zhivye-obnovleniya.md) | Presence and the event bus over Redis: how an edit shows up for everyone |
| [13 — Telegram inside the CRM](docs/ustroystvo/13-telegram-v-crm.md) | Company bot, dialogues, linking to clients |
| [15 — Backups from the settings](docs/ekspluatatsiya/15-kopii-s-shifrovaniem.md) | Encrypted copy of the database and files, restore from the screen |
| [16 — Shop-site API](docs/ustroystvo/16-api-sayta.md) | Keys and scopes, catalog, availability, reservations: the reasoning |
| [17 — Waybills](docs/bloki/17-nakladnye.md) | Paper, stock moves, immutability |
| [19 — Order assembly](docs/bloki/19-sborka-zakaza.md) | Picking an order from one place |
| [20 — Usability and in-app guide](docs/dizayn/20-udobstvo-i-spravka.md) | Live dashboard, sorting, the documentation screen |
| [21 — Module links](docs/bloki/21-svyaz-blokov.md) | Papers issued by themselves, deleting papers created by mistake, notifications |
| [22 — Customer returns](docs/bloki/22-vozvraty.md) | Paper, stock, money |
| [26 — Client address](docs/bloki/26-adresa.md) | Suggestions while typing and a point on the map |
| [27 — Keys](docs/bloki/27-klyuchi.md) | Second-factor (TOTP) vault |
| [27 — Sales report](docs/dizayn/27-otchyot-prodazh.md) | Dashboard sales widget |
| [28 — Files](docs/bloki/28-fayly.md) | One tree for everything on disk |
| [24 — Security audit](docs/ekspluatatsiya/24-audit-bezopasnosti.md), [14 — Rust](docs/ustroystvo/14-rust.md), [18 — Third-party components](docs/dizayn/18-chuzhie-komponenty.md), [23 — Kubernetes note](docs/ekspluatatsiya/23-kubernetes-zametka.md) | One-off reviews and decisions |

## 📁 Project structure

```
OpenCRM/
├── opencrm.sh          # installer and server management menu
├── config/             # settings (OPENCRM_*), .env template, self-check
├── core/               # business logic: services, permissions, modules, live updates, security
├── database/           # models, repositories (all queries live here), Alembic migrations
├── web/
│   ├── main.py         # FastAPI app, /healthz
│   ├── api/            # REST API
│   ├── public/         # public showcase and client-facing pages
│   └── frontend/crm/   # React + TypeScript SPA (Vite)
├── deploy/             # auto-updater and systemd units (run on the host)
├── docker/             # Dockerfile, compose stack, nginx, monitoring
├── scripts/            # backup/restore, reset_root, seed_demo, maintenance tools
├── nagruzka/           # load-testing scripts
├── tests/              # pytest suite (runs against MySQL)
└── docs/               # manual (Russian), screenshots, logos
```

## 🤝 Contributing

Issues and pull requests are welcome. Read [`CLAUDE.md`](CLAUDE.md) first (in Russian): a change to `database/models/` ships with its Alembic migration in the same commit, queries stay inside `database/`, and the test suite must pass against MySQL.

## 📄 License

License: not specified yet.

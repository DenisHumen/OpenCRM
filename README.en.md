<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo.svg" alt="OpenCRM" width="260">
</picture>

*[Русский](README.md)*

A CRM for a design studio, with a public showcase of its work.

Two worlds in one product:

- **CRM** — the team's internal tool: client database, notes, interaction history, files, boards of works.
- **Showcase** — a public one-page portfolio: a manager assembles a board and shares a link; the client opens it without signing up and sees a properly presented portfolio.

## Principles

- Backend in Python (FastAPI), frontend in React + TypeScript.
- MySQL 8 for the data, with Redis alongside; the schema is brought up to date by migrations on its own (SQLAlchemy + Alembic).
- Work files live on the server, next to the application.
- Clients never register — boards are reachable by link only, with an optional PIN and expiry date.
- Staff sign up themselves; accounts are approved by root, the built-in superuser.
- The interface is English by default; every user can switch, and the choice is remembered.

## Documentation

Everything lives in [docs](docs/README.md). **The documentation is written in Russian** — this file is the English entry point to the project, not a translation of the whole manual.

| Document | Contents |
|---|---|
| [01 — Overview](docs/01-overview.md) | Concept, roles, usage scenarios |
| [02 — Architecture](docs/02-architecture.md) | Stack, directory layout, modules, media pipeline |
| [03 — Database](docs/03-database.md) | Schema, tables, migrations |
| [04 — API](docs/04-api.md) | REST API specification |
| [05 — CRM design](docs/05-crm-design.md) | Design notes for the CRM interface |
| [06 — Showcase design](docs/06-showcase-design.md) | Public showcase concept, animations, controls |
| [07 — Security](docs/07-security.md) | Authentication, roles, public links, file protection |
| [08 — Deployment](docs/08-deployment.md) | Docker, VPS, backups, auto-update |
| [09 — Roadmap](docs/09-roadmap.md) | Delivery stages |

## Status

Stages 0–5 of the roadmap are done (see [09-roadmap.md](docs/09-roadmap.md)): a backend with integration tests, the React SPA built to the mockups, and a public showcase matching them 1:1 (modular grid, blurhash, PIN cells, lightbox). Production artefacts are ready: Docker, nginx, backups, maintenance scripts, and auto-update from GitHub with rollback. What remains is the VPS deployment itself, with HTTPS and external monitoring — the steps are below.

## Quick start (dev)

```bash
python -m venv .venv
.venv/Scripts/pip install -e . --group dev
.venv/Scripts/python -m alembic upgrade head
cd web/frontend/crm && npm install && npm run build && cd ../../..
.venv/Scripts/python -m uvicorn web.main:app --host 0.0.0.0 --port 8000
```

- The CRM opens at `http://localhost:8000/` (FastAPI serves the built SPA); showcases live at `/b/{token}`.
- **`--host 0.0.0.0` is required if you open the app from another machine.** By default uvicorn listens on `127.0.0.1` only, and `http://192.168.x.x:8000` from a phone or a neighbouring computer will not connect at all. To show a showcase over the local network, also set `OPENCRM_BASE_URL=http://192.168.x.x:8000` — otherwise the board link copied inside the CRM points at `localhost` and will not open for your colleague. On Windows the first run with `0.0.0.0` asks for a firewall exception; grant it for the "Private" network.
- Before starting, copy `config/.env.example` to `config/.env` and fill it in. In `production` the app refuses to start with an empty `OPENCRM_SECRET_KEY` or `OPENCRM_IP_HASH_SALT` — that is cookie forgery protection, not pedantry.
- The root account is created **once**, on an empty database, from `OPENCRM_ROOT_EMAIL`/`OPENCRM_ROOT_PASSWORD`; the first sign-in forces a password change. After that, editing those variables changes nothing — use the command instead:

```bash
python scripts/reset_root.py --email me@studio.site --password "new-password"
```
- Frontend with hot reload: `npm run dev` in `web/frontend/crm` (Vite on 5173, API proxied to 8000).
- API docs (dev): `http://localhost:8000/api/docs`.
- Tests: `.venv/Scripts/python -m pytest`.
- Demo data and a sample showcase: `.venv/Scripts/python scripts/seed_demo.py` (the server must be running).

## Deploying to a server (Ubuntu 24.04)

One script does everything. All it needs from you is a domain.

```bash
sudo apt install -y git
sudo git clone https://github.com/DenisHumen/OpenCRM.git /opt/OpenCRM
sudo chown -R $USER:$USER /opt/OpenCRM
cd /opt/OpenCRM && ./opencrm.sh
```

The very first question is the language — Russian or English. The choice is saved in `docker/.env` (`OPENCRM_LANG`), so the menu, the diagnostics and anything cron prints all speak the same way. To change it later, edit that variable or set `OPENCRM_LANG=en` in the environment.

After that, on its own the wizard:

1. **installs Docker** from Docker's own repository (`apt install docker.io` ships without the `compose` v2 plugin the project depends on) and adds you to the `docker` group;
2. **generates secrets** — `OPENCRM_SECRET_KEY`, the IP hashing salt, and the admin password; a second run leaves them alone, since regenerating them would sign everyone out and void every PIN link already handed out;
3. **substitutes your UID:GID** into `docker/.env` — the container writes into mounted directories as that user, and a mismatch means "permission denied" on the very first migration;
4. **creates the state directories** under `~/opencrm/`;
5. **builds and starts the stack**, then waits for `/healthz`;
6. **issues a Let's Encrypt certificate** — after checking that the domain's A record actually points at this server (otherwise the challenge fails anyway and burns one of the weekly attempts);
7. **closes the firewall** — ufw lets in SSH and the site, nothing else. The SSH port is detected from three sources at once (the live connection, listening sockets, the config), so the script cannot lock you out of your own server: on Ubuntu 24.04 ssh is socket-activated, and the `Port` in `sshd_config` may have nothing to do with reality;
8. **schedules daily backups** at 03:30 — via a systemd timer, or a cron job where there is no systemd;
9. **enables auto-update** — as a systemd service, or a cron job without systemd.

At the end it prints the address, the login and the admin password. The password is shown once; the first sign-in asks you to change it.

Before building, the script checks memory and disk space: the frontend build is the hungriest step of the install, and on a 1 GB machine with no swap the OOM killer takes it out without any useful message. If memory is short, the script offers to add a swap file.

The domain is optional — without it the site comes up over HTTP and is reachable by IP on the local network. To add a domain later: `./opencrm.sh domain`.

Non-interactively (from Ansible, say):

```bash
./opencrm.sh install --domain studio.example --email you@studio.example --yes
```

### After that — the menu

Running `./opencrm.sh` again opens a menu:

```
   1) Status and health         8) Logs
   2) Start                     9) Backup
   3) Restart                  10) Restore from backup
   4) Stop                     11) Domain and HTTPS
   5) Update now               12) Firewall
   6) Auto-update on/off       13) Reset admin password
   7) Update journal           14) Diagnostics
                                0) Exit
```

Everything is available as commands too — for cron and scripts:

```bash
./opencrm.sh status          # what is deployed, is it alive, is there an update
./opencrm.sh update          # update now, without waiting for the poll
./opencrm.sh autoupdate off  # pause before manual work
./opencrm.sh backup
./opencrm.sh firewall        # inspect and repair the ufw rules
./opencrm.sh doctor          # environment check for when something is off
./opencrm.sh maintenance off # reopen the site if an interrupted move closed it
```

### While the site is updating

Replacing the container takes seconds, but the application is gone for all of them. Instead of a bare "502 Bad Gateway" nginx serves a maintenance page — `docker/nginx/maintenance/maintenance.html`, which you can edit to taste. It answers with status 503 and a `Retry-After` header: search engines keep such pages in the index instead of dropping them as broken. The page brings the visitor back on its own as soon as the app answers again.

There is a snake game hidden in its footer, for the minute of waiting. While the site is down there is nowhere to send a score, so it is kept in `localStorage` and submitted the moment the application answers; the leaderboard lives in the main database. No extra service runs in production for the sake of a game.

The application's own errors (404, 500) are never replaced by the maintenance page, and the Let's Encrypt challenge keeps working during an update — otherwise certificate renewal would fail silently one day.

### Closing the site by hand

This is a different thing: the application is running, but root has closed it for work — "Site settings → Maintenance". Everyone else gets the maintenance page: staff and clients with board links alike. Root keeps using the CRM as usual, since otherwise there would be nowhere to switch the mode back off.

An optional note can be left — "migrating the database, back by 14:00" — and it appears on the page everyone else sees. Reopening the site clears the note: one left over from last time would mislead more than no note at all.

While the mode is on, a strip across the top of the interface reminds root about it and says who closed the site and when. Without it, a forgotten mode silently keeps both the showcases and other people's access closed. `/healthz` keeps answering `200` throughout — otherwise Docker would consider the container sick and start restarting it.

### Worth knowing in advance

- **Editing files directly on the server stops auto-update.** It sees a dirty working tree and refuses to move: overwriting your work is not its call. Either commit, or `git checkout -- .`, or `./opencrm.sh autoupdate off` while you work.
- **A failed commit is not retried in a loop.** It is remembered, and the daemon waits for the next one; to retry that same commit, run `./opencrm.sh update`.
- **Rollback restores the database only if the container was already replaced.** Had the build failed earlier, the old application was serving and accepting writes all along, and reverting to the snapshot would erase them. Snapshots live in `~/opencrm/updates/`.
- **Backups sit on the same disk as the database.** That saves you from a corrupted database and from your own mistake, but not from the disk dying. Off-site upload is configured in `scripts/backup.sh` — there is a worked example in the file.
- **Docker publishes ports around ufw.** 80 and 443 will be open even if a rule forbids them — that is how Docker works, and for the site it is exactly what you want. But any other container with `ports:` will be exposed against your rules too: publish those on `127.0.0.1` only.

How it all works inside, and why it was built that way — [08-deployment.md](docs/08-deployment.md) (in Russian).

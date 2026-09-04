<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo.svg" alt="OpenCRM" width="260">
</picture>

*[Русская версия](README.ru.md)*

**A CRM for a small business: jobs, clients, stock, paperwork and money in one
place — on your own server, no subscription, no third-party cloud.**

One command installs it on a $5 VPS. After that it updates itself, backs itself
up, and never phones home.

![Request board](docs/images/requests-board.png)

---

## Who it's for

A sign shop, a repair bench, a boutique, a studio, a salon — anyone doing five
to fifty jobs a week without an IT department.

You shape it with **switches, not custom work**. On first sign-in it asks what
your business does and turns on the parts that fit. No stock to track? That
section is gone — out of the menu, out of the API. Start carrying stock later
and one switch brings it back with your data intact.

Everything below is a screenshot of the real thing. The data is invented: a
Portland sign shop called Beacon Sign & Print, its made-up customers and jobs.

---

## Jobs on a board

The backbone. Every job carries a stage, an owner, a due date, an amount, what's
been paid, and a record of who moved it when.

![Dragging a card between stages](docs/images/drag-stage.gif)

You don't draw the pipeline from scratch. Five ready-made stage sets ship with
it — services and repair, retail, appointments, agency, general-purpose — and
every stage renames, moves or goes away. The one rule the system enforces: keep
one winning end and one losing end, or there's nothing to measure against.

Even the word is yours. Call them deals, orders, requests or bookings; every
label in the app follows. The screenshots here say *requests*, because that's
what a sign shop calls them.

![Request card](docs/images/request-card.png)

---

## Clients

A client card is a single feed — calls, meetings, emails, notes. Receipts you
issued and materials you burned on their job drop into the same place, so
"what have we actually done for these people" is one screen instead of three.

![Client card](docs/images/client-card.png)

---

## Finding things

Ctrl+K anywhere. Clients, jobs and boards in one list; an empty box shows what
you touched last. Search obeys permissions — if someone can't see it, they
can't find it either.

![Command palette](docs/images/search.gif)

---

## Stock

Several locations. Receipts, issues, write-offs, returns, stock-take
corrections, transfers between stores. Quantities carry three decimal places,
so 0.125 kg and 1.5 m² survive the round trip.

![Recording a receipt](docs/images/stock-receipt.gif)

**Nothing stores the balance.** It's the sum of the movements, worked out on
demand. A stored total drifts from its own history eventually, and then nobody
can say which number to trust. Job costing works the same way, for the same
reason.

![Warehouse](docs/images/warehouse.png)

---

## Barcodes

One item, several codes: a single, a pack of ten, a full box. Scan the box and
ten units land in the order. The barcodes are drawn by the app itself — nothing
is fetched from anywhere — and a scanner just types, so there are no drivers to
install and no browser prompts to click through.

![Product card with a barcode](docs/images/product-card.png)

---

## Orders

Sales orders and purchase orders. Lines get picked by scanning, and what you
picked stays separate from what was ordered — so a short line shows up on the
bench, not at the customer's door.

![Orders](docs/images/orders.png)

---

## Paperwork

Intake receipts, completion certificates, order forms. Numbering runs
continuously through the year, and printing speaks English, Russian or
Ukrainian — picked for the customer in front of you, not for the person typing.

![Printed form](docs/images/printed-form.png)

Every form carries a barcode and a QR code. The barcode pulls the job up on a
scan. The QR code sends the customer to a page where they can check on it
themselves, which is one phone call you don't take.

![Forms](docs/images/forms.png)

---

## Money

Income and spending by category, budgets per period, profit across any range.
Amounts are whole numbers of cents and never touch a `float`.

![Finance](docs/images/finance.png)

**Entries can't be edited or deleted** — there is no such permission. You fix a
mistake with an opposing entry, the way a paper cash book works, so what
happened stays legible instead of quietly becoming what you wish had happened.

---

## Reports

Where jobs stall, what you billed month by month, and which channels actually
bring customers. Every one exports to CSV.

![Reports](docs/images/reports.png)

---

## Showing work to a customer

Put a board together, get a link, send it. Your customer opens it with no
account and sees your work presented properly, not a shared folder.

![Public showcase](docs/images/showcase.png)

Links take an access code and an expiry date. The customer types the code once
and the pass lives in their browser after that.

![Entering the code](docs/images/showcase-pin.gif)

Back in the CRM the board tells you everything: published or not, how many
times it was opened, by how many different people, and when. Revoke a link or
reissue it — the old one dies, the work stays.

![Board editor](docs/images/board-editor.png)

---

## Turning things off

Fifteen modules. Two of them hold the place up — Clients and Deals — and the
rest come and go one switch at a time.

![Switching a module off](docs/images/modules-off.gif)

A module that's off is gone completely: menu, API, reports, search, permissions
matrix. **Nothing is deleted.** Switch it back on and your data is exactly where
you left it. Dependencies are handled for you — turn off the warehouse and
you're told that labels go with it, before anything happens.

The full list: Clients, Deals, Companies, Forms, Reminders, Message templates,
Boards, Warehouse, Labels, Orders, Reports, Mail, Calls, Finance, Monitoring.

---

## Who can see what

A role is a job title with a set of permissions. Five come ready — manager,
accountant, project manager, director, observer — and you can rebuild any of
them.

![Roles](docs/images/roles.png)

Permissions restrict the server, not the screen. Without the right to see
amounts, they don't reach the browser at all: not in a list, not in a report,
not in a CSV export. Other people's jobs are filtered inside the query rather
than hidden with CSS.

---

## Dashboard

Money in progress, what closed this month, average job size, the pipeline,
today's reminders, and who's been opening your boards.

![Dashboard](docs/images/dashboard.png)

---

## Also in the box

- **Mail.** A company IMAP/SMTP mailbox; incoming messages match a client by
  address and land in their feed.
- **Calls.** A call log, a signed webhook from your phone provider,
  click-to-call from any card, and a "ring them back" reminder in one click.
- **Leads from your website.** Post your contact form to a keyed endpoint and
  the enquiry arrives in the pipeline.
- **Server monitoring.** Prometheus, Grafana, Loki and Telegram alerts, behind a
  compose profile — skip it entirely on a small box.
- **Activity log** for root: who changed what, read-only.
- **Maintenance mode.** Visitors get a holding page; root keeps working.
- **Two interface languages** — English and Russian, per user.
- **Shop-site API.** Keyed endpoints for a storefront or marketplace: catalogue,
  stock of one shop warehouse, a change feed, orders that reserve stock for a
  while, customer sign-ups. Keys have scopes, limits and an expiry.
- **Backups from the settings screen.** Encrypted copies of the database and
  of the files, downloaded by hand; every copy is checked with the current key
  right after it is taken. Restore is a separate right and a guarded procedure.
- **Live updates.** A colleague's change shows up on your open screen by
  itself — board, cards, lists, dashboard. One switch turns it off.
---

## Installing it

An Ubuntu 24.04 server, and a domain if you have one. Then one command.

```bash
sudo apt install -y git
sudo git clone https://github.com/DenisHumen/OpenCRM.git /opt/OpenCRM
sudo chown -R $USER:$USER /opt/OpenCRM
cd /opt/OpenCRM && ./opencrm.sh
```

`chown` isn't cosmetic: the clone runs under `sudo`, so the directory ends up
owned by root while the script and the auto-updater run as you. Without it git
says `detected dubious ownership` and refuses to touch anything. You don't need
`chmod +x` — the executable bit is in the repository.

The first question is the language, English or Russian. It's saved to
`docker/.env` (`OPENCRM_LANG`), so the menu, the diagnostics and anything cron
mails you all speak the same way. Change it later by editing that variable.

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

At the end it prints the address, the login and the admin password. The password
appears once; the first sign-in makes you change it.

Before building it checks memory and disk. The frontend build is the hungriest
step, and on a 1 GB box with no swap the OOM killer takes it out without saying
why — so if memory is tight, the script offers to add a swap file.

No domain is fine: the site comes up over HTTP and answers on its IP. Add one
later with `./opencrm.sh domain`.

Non-interactively, from Ansible or similar:

```bash
./opencrm.sh install --domain studio.example --email you@studio.example --yes
```

### After that, a menu

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

---

## How it's built

- Python (FastAPI) on the back, React + TypeScript single-page app on the front.
- MySQL 8 for data, Redis beside it holding the sign-in and PIN attempt counters shared across processes.
- Migrations (SQLAlchemy + Alembic) run on every update — after the database is copied, before the app starts.
- An app whose schema doesn't match **refuses to start**: `/healthz` never returns 200, so the update rolls back both code and database. There's no "running on the wrong schema" state to discover later.
- Uploaded work lives on the server next to the app; previews, blurhashes and video posters are made at upload time.
- Money is integer minor units, quantities are integer thousandths. Neither goes near a `float`.
- Every database query lives in `database/`. A test enforces that none escape.
- Nothing calls out: no telemetry, no third-party update checks, no external CDNs. The CSP wouldn't allow it anyway.

### Updating

The server updates itself: a daemon pulls the commit, rebuilds the container,
copies the database and applies migrations before start. While the container is
swapped, nginx serves a holding page with a 503 and `Retry-After` — search
engines keep those pages indexed instead of dropping them as broken.

- **Editing files on the server stops auto-update.** It sees a dirty working tree and won't move; overwriting your work isn't its call. Commit, or `git checkout -- .`, or `./opencrm.sh autoupdate off` while you work.
- **A failed commit isn't retried in a loop.** It's remembered and the daemon waits for the next one. To retry that same commit, `./opencrm.sh update`.
- **Rollback restores the database only if the container was already swapped.** If the build failed earlier than that, the old app was serving and taking writes the whole time, and restoring the snapshot would erase them. Snapshots are in `~/opencrm/updates/`.
- **Backups sit on the same disk as the database.** That covers a corrupted database and your own mistakes, not a dead disk. Off-site upload is configured in `scripts/backup.sh`, worked example included.
- **Docker publishes ports around ufw.** 80 and 443 stay open even if a rule forbids them — that's Docker, and for the site it's what you want. But any other container with `ports:` is exposed the same way, so publish those on `127.0.0.1` only.

### Developing

```bash
python -m venv .venv
.venv/Scripts/pip install -e . --group dev
.venv/Scripts/python -m alembic upgrade head
cd web/frontend/crm && npm install && npm run build && cd ../../..
.venv/Scripts/python -m uvicorn web.main:app --host 0.0.0.0 --port 8000
```

- The CRM opens at `http://localhost:8000/`; showcases live at `/b/{token}`.
- **`--host 0.0.0.0` is required to reach it from another machine.** uvicorn binds `127.0.0.1` otherwise. To show a board over the LAN, also set `OPENCRM_BASE_URL=http://192.168.x.x:8000`, or the link you copy will point at `localhost`.
- Copy `config/.env.example` to `config/.env` first. In `production` the app won't start with an empty `OPENCRM_SECRET_KEY` or `OPENCRM_IP_HASH_SALT` — that's cookie forgery protection, not pedantry.
- The root account is created **once**, on an empty database, from `OPENCRM_ROOT_EMAIL`/`OPENCRM_ROOT_PASSWORD`. After that:

```bash
python scripts/reset_root.py --email me@studio.site --password "new-password"
```

- Frontend with hot reload: `npm run dev` in `web/frontend/crm` (Vite on 5173, API proxied to 8000).
- API docs (dev only): `http://localhost:8000/api/docs`.
- Tests: `.venv/Scripts/python -m pytest`.
- Demo data and a sample showcase: `.venv/Scripts/python scripts/seed_demo.py`, with the server running.

---

## Documentation

Everything is in [docs](docs/README.md). **The manual is written in Russian** —
this file is the English way into the project, not a translation of all of it.

| Document | Contents |
|---|---|
| [01 — Overview](docs/01-overview.md) | What the system does, roles, scenarios |
| [02 — Architecture](docs/02-architecture.md) | Stack, directory layout, modules, media pipeline |
| [03 — Database](docs/03-database.md) | Schema, tables, migrations, indexes |
| [04 — API](docs/04-api.md) | REST API specification |
| [05 — CRM design](docs/05-crm-design.md) | Design notes for the interface |
| [06 — Showcase design](docs/06-showcase-design.md) | Public showcase, animation, controls |
| [07 — Security](docs/07-security.md) | Authentication, roles, public links, file protection |
| [08 — Deployment](docs/08-deployment.md) | Docker, VPS, backups, auto-update |
| [09 — Roadmap](docs/09-roadmap.md) | Delivery stages |
| [11 — Modules](docs/11-modules.md) | Module registry, dependencies, permissions |
| [12 — Live updates](docs/12-realtime.md) | Presence and the event bus over Redis: how an edit shows up for everyone |
| [13 — Telegram inside the CRM](docs/13-telegram-messenger.md) | Company bot, dialogues, linking to clients |
| [15 — Backups from the settings](docs/15-backup-encryption.md) | Encrypted copy of the database and files, restore from the screen |
| [16 — Shop-site API](docs/16-api-sayta.md) | Keys and scopes, catalog, availability, reservations: the reasoning |
| [17 — Waybills](docs/17-nakladnye.md) | Paper, stock moves, immutability |
| [19 — Order assembly](docs/19-sborka-zakaza.md) | Picking an order from one place |
| [20 — Usability and in-app guide](docs/20-udobstvo-i-spravka.md) | Live dashboard, sorting, the documentation screen |
| [21 — Module links](docs/21-svyaz-blokov.md) | Papers issued by themselves, deleting papers created by mistake, notifications |
| [10 — Security audit](docs/10-security-audit.md), [14 — Rust](docs/14-rust.md), [18 — Third-party components](docs/18-chuzhie-komponenty.md) | One-off reviews and decisions |

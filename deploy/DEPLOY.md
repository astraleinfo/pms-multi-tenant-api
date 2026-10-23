# Deploying PMS on the shared server

PMS runs next to dentglitz on the same server without touching it. dentglitz
keeps its containers, ports and nginx site exactly as they are.

## What ends up where

| Address | What it is |
|---|---|
| `https://pms.astraleinfo.com` | Landing page |
| `https://admin.pms.astraleinfo.com` | Super-admin console |
| `https://<clinic>.pms.astraleinfo.com` | One clinic each |
| `https://<any of the above>/api/...` | The API (same address, under `/api`) |

| On the server | Used by |
|---|---|
| `~/project/pms-multi-tenant-api` | API, database, Redis, worker, beat, Flower |
| `~/project/pms-multi-tenant-web` | Web app |
| `127.0.0.1:18000` | API, reachable from the server only (`PMS_API_PORT`, Step 2) |
| `127.0.0.1:13000` | Web app, reachable from the server only (`PMS_WEB_PORT`, Step 2) |
| `/etc/nginx/sites-enabled/pms.conf` | The PMS site on the existing nginx |
| `/etc/nginx/ssl/pms.astraleinfo.com/` | Wildcard certificate |

```
browser ──443──> host nginx ──┬── dentglitz.com      → dentglitz (unchanged)
                              └── *.pms.astraleinfo.com
                                     ├── /api/* → 127.0.0.1:18000 → PMS API → PMS db, PMS redis
                                     └── /*     → 127.0.0.1:13000 → PMS web
```

Run every server command as `root`.

---

## Part A: First-time setup

Follow the steps in order. Each one ends with what you should see.

### Step 1: Push the code (on your laptop)

The server clones from GitHub, so everything must be pushed first.

```bash
cd ~/astrale/pms-multi-tenant-api && git status     # commit and push anything listed
cd ~/astrale/pms-multi-tenant-web && git status     # same
```

**Expected:** `nothing to commit, working tree clean` in both, and
`Your branch is up to date with 'origin/main'`.

### Step 2: Check the server and pick two free ports

PMS publishes two ports on the server, both on `127.0.0.1` only: one for the API and one
for the web app. They must not be in use by anything else. The defaults are **18000**
(API) and **13000** (web).

These are already taken on this server, so never use them for PMS:

| Port | Used by |
|---|---|
| `80`, `443` | host nginx |
| `4000` | dentglitz-web |
| `9000` | dentglitz-api |
| `5555` | dentglitz Flower |

List everything currently listening, then check the two defaults:

```bash
ss -ltn | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$' | sort -n | uniq
ss -ltn | grep -E ':(18000|13000)\b'
```

**Expected:** the second command prints **nothing**, which means 18000 and 13000 are free.
Use them, and skip ahead to the IP check below.

**If either one is listed**, pick another port that isn't in the first list (for example
18100 or 13100), confirm it's free with the same `ss -ltn | grep` check, and write the
numbers down. You'll set them in three places:

| Port | Where |
|---|---|
| API | `PMS_API_PORT=` in the API `.env` (Step 7) |
| Web | `PMS_WEB_PORT=` in the web `.env` (Step 11) |
| Both | the nginx command in Step 12 |

The rest of this guide uses 18000 and 13000. Swap in your numbers wherever they appear.

```bash
curl -4 -s ifconfig.me; echo             # the server's public IP, for DNS
free -h                                  # memory
```

**Expected:** `curl` prints an IP address. Write it down; Step 4 needs it. Don't use
`dentglitz.com`'s IP: that one belongs to Cloudflare.

Optional: the web build can use 1–2 GB of memory, and the server has no swap.
This adds 2 GB:

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### Step 3: Give the server access to the PMS repos

First test whether the existing key already works:

```bash
git ls-remote git@github.com:astraleinfo/pms-multi-tenant-api.git HEAD
git ls-remote git@github.com:astraleinfo/pms-multi-tenant-web.git HEAD
```

**If both print a commit hash,** skip to Step 4 and clone with `git@github.com:` in Step 6.

**If either says `Permission denied` or `Repository not found`,** the key is a deploy key
for dentglitz only. Create one key per PMS repo:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/pms_api -N ""
ssh-keygen -t ed25519 -f ~/.ssh/pms_web -N ""
cat >> ~/.ssh/config <<'EOF'

Host github-pms-api
  HostName github.com
  IdentityFile ~/.ssh/pms_api
  IdentitiesOnly yes

Host github-pms-web
  HostName github.com
  IdentityFile ~/.ssh/pms_web
  IdentitiesOnly yes
EOF
cat ~/.ssh/pms_api.pub     # add on GitHub: pms-multi-tenant-api → Settings → Deploy keys
cat ~/.ssh/pms_web.pub     # add on GitHub: pms-multi-tenant-web → Settings → Deploy keys
```

Leave **Allow write access** unchecked. Then run the two `git ls-remote` commands
again, with `github-pms-api` / `github-pms-web` in place of `github.com`.

### Step 4: DNS records (Netlify)

Open Netlify → **Domains** → `astraleinfo.com` → **DNS settings**.

| Name | Type | Value |
|---|---|---|
| `pms` | A | the IP from Step 2 |
| `*.pms` | A | the IP from Step 2 |
| `admin.pms` | A | the IP from Step 2 (optional; `*.pms` already covers it) |
| `pms-api` | delete it | not used |

- In the **Name** field type just `pms`, `*.pms`, `admin.pms`. Netlify adds `.astraleinfo.com` itself.
- If Netlify won't let you edit or delete `pms` / `pms-api`, a Netlify site is using them.
  Open that site → **Domain management** and remove them there first. Make sure nothing
  else depends on them.

Check from any machine:

```bash
dig +short pms.astraleinfo.com
dig +short admin.pms.astraleinfo.com
dig +short test.pms.astraleinfo.com
```

**Expected:** all three print the server's IP. It can take a few minutes after saving.
**Don't continue until they do.**

### Step 5: Wildcard certificate

Follow **[NGINX.md](NGINX.md), Part 1**. It issues one certificate for
`pms.astraleinfo.com` and every `*.pms.astraleinfo.com`, and sets it up to renew itself.
It needs the DNS records from Step 4 and nothing else, so do it now: the certificate is
ready by the time nginx needs it in Step 12.

**Expected:** `/etc/nginx/ssl/pms.astraleinfo.com/` holds `fullchain.pem` and
`privkey.pem`, and dentglitz carries on exactly as before.

### Step 6: Clone both repos

```bash
cd ~/project
git clone git@github.com:astraleinfo/pms-multi-tenant-api.git
git clone git@github.com:astraleinfo/pms-multi-tenant-web.git
ls
```

If Step 3 needed new keys, use `git@github-pms-api:` and `git@github-pms-web:` in place
of `git@github.com:`.

**Expected:** `dentglitz-api  dentglitz-web  pms-multi-tenant-api  pms-multi-tenant-web`.
Keep the folder names exactly as they are: Docker names containers and volumes after them.

### Step 7: Create the `.env` file

This one file holds every setting and password for the API stack. The web app has its
own one-line `.env`, created in Step 11.

**If you prepared `.env` on your laptop**, copy it across and skip the rest of this step
except the `chmod`:

```bash
# on your laptop
scp ~/astrale/pms-multi-tenant-api/.env root@<server-ip>:~/project/pms-multi-tenant-api/.env
```

**Otherwise**, build it on the server:

```bash
cd ~/project/pms-multi-tenant-api
cp .env.example .env

# Generate the secrets, then copy each value into .env
echo "JWT_SECRET=$(openssl rand -hex 32)"
echo "SECRETS_KEY=$(openssl rand -hex 32)"
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
echo "PMS_OWNER_PASSWORD=$(openssl rand -hex 24)"
echo "PMS_APP_PASSWORD=$(openssl rand -hex 24)"
echo "FLOWER_BASIC_AUTH=pms-console:$(openssl rand -hex 16)"

nano .env
```

In `.env`, change these lines (find each one; add any line that's missing):

```bash
ENVIRONMENT=production
DEBUG=false

APP_DOMAIN=pms.astraleinfo.com
PUBLIC_SCHEME=https
PUBLIC_PORT=
COOKIE_SECURE=true
COOKIE_DOMAIN=.pms.astraleinfo.com
REFRESH_COOKIE_PATH=/api/v1/auth
ALLOW_TENANT_HEADER=false

JWT_SECRET=<generated>
SECRETS_KEY=<generated>

MAIL_PROVIDER=smtp
MAIL_FROM=Astrale PMS <no-reply@astraleinfo.com>
SMTP_HOST=<your SMTP server>
SMTP_PORT=587
SMTP_USER=<SMTP user>
SMTP_PASSWORD=<SMTP password>
SUPPORT_EMAIL=<where support requests go>

POSTGRES_PASSWORD=<generated>
PMS_OWNER_PASSWORD=<generated>
PMS_APP_PASSWORD=<generated>
FLOWER_BASIC_AUTH=pms-console:<generated>

PMS_API_PORT=18000          # the API port from Step 2
```

You don't need to set `DATABASE_URL`, `MIGRATION_DATABASE_URL` or `REDIS_URL`.
`docker-compose.yml` builds them from the passwords above and ignores anything `.env` says for them.

```bash
chmod 600 .env
```

> **Get the passwords right before Step 8.** The database takes them on its first start
> only. Editing them in `.env` later does not change them in the database.

### Step 8: Start the database

The database is created here, automatically. Nothing to create by hand.

```bash
cd ~/project/pms-multi-tenant-api
docker compose up -d db
docker compose ps db
```

Wait until the status says `(healthy)`, usually under 30 seconds. Then check:

```bash
docker compose logs db | grep 10-bootstrap
docker compose exec db psql -U postgres -c '\du'
docker compose exec db psql -U postgres -l
```

**Expected:**
- The log shows `running /docker-entrypoint-initdb.d/10-bootstrap.sh`.
- `\du` lists `pms_owner` and `pms_app`.
- `-l` lists a database called `pms`.

If something is wrong (a typo in a password, the bootstrap failed), reset the **still-empty** database and retry:

```bash
docker compose down
docker volume rm pms-multi-tenant-api_pms-pgdata
docker compose up -d db
```

### Step 9: Start the API stack

This builds the image, creates the tables (migrations), then starts the API, worker,
beat and Flower.

```bash
cd ~/project/pms-multi-tenant-api
APP_RELEASE=$(git rev-parse --short HEAD) docker compose up -d --build
docker compose ps -a
```

**Expected** (after about a minute):

| Service | Status |
|---|---|
| `db`, `redis`, `api` | `Up … (healthy)` |
| `worker`, `beat`, `flower` | `Up …` |
| `migrate` | `Exited (0)` |

`Exited (0)` is correct for `migrate`: it runs the migrations once and stops.

```bash
docker compose logs migrate | tail -2
curl -s 127.0.0.1:18000/health; echo
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:18000/health/db
```

**Expected:** the migrate log ends with `... -> 0026_merge_heads` (or a later revision),
then `{"status":"ok"}`, then `200`.

With `STORAGE_BACKEND=s3`, check that the keys in `.env` can reach the bucket:

```bash
docker compose run --rm --no-deps api python -c "import boto3, os; boto3.client('s3', region_name=os.environ['AWS_REGION']).head_bucket(Bucket=os.environ['S3_BUCKET']); print('bucket ok')"
```

**Expected:** `bucket ok`. A `403` means the keys aren't allowed to use that bucket;
a `404` means the bucket doesn't exist in that region.

### Step 10: Create the super admin

This loads the catalogues every clinic needs (clinical types, lab tests, medicines, form
fields) and creates your super-admin account. It doesn't load any demo data.

```bash
cd ~/project/pms-multi-tenant-api
docker compose run --rm migrate python scripts/seed.py --production \
  --email astraleinfo@gmail.com --name "Astrale Info"
```

It asks for the password twice (at least 12 characters).

**Expected:** `seeded: catalogues=1, super_admins=1`

- Run it **once**. Running it again with another email adds a second super admin;
  it never deletes anything.
- **Never run `scripts/seed.py` without `--production` on the server.** That's the
  demo seed: it wipes every table. It refuses to run with `ENVIRONMENT=production`,
  but don't rely on that.

### Step 11: Start the web app

The web app needs one setting: the base domain. Without it, `pms.astraleinfo.com` would
be read as a clinic called `pms`.

Leave out the other variables in `.env.local.example`. They're for local development:

| Variable | In production |
|---|---|
| `NEXT_PUBLIC_API_BASE` | Not set. The browser calls `/api` on the address it's on, e.g. `https://demo.pms.astraleinfo.com/api/...` |
| `NEXT_PUBLIC_API_WS_ORIGIN` | Not set. The live queue opens `wss://demo.pms.astraleinfo.com/api/v1/ws/...` by itself |
| `API_PROXY_TARGET` | Not set. nginx sends `/api` to the API before the web app sees it |

Setting any of them to another address, such as `127.0.0.1:8000` or a separate API domain,
breaks sign-in or the live queue: the clinic is read from the address, and a different
address loses it.

```bash
cd ~/project/pms-multi-tenant-web
printf 'NEXT_PUBLIC_APP_DOMAIN=pms.astraleinfo.com\nPMS_WEB_PORT=13000\n' > .env
cat .env
REVISION=$(git rev-parse HEAD) docker compose up -d --build
docker compose ps
curl -sI 127.0.0.1:13000/ | head -1
```

**Expected:** the build takes a few minutes. Then `web` is `Up … (healthy)` and
`curl` prints `HTTP/1.1 200 OK`.

The domain is built into the app. If you ever change `.env` here, rebuild with
`docker compose up -d --build`; a plain restart keeps the old value.

### Step 12: Connect nginx

Follow **[NGINX.md](NGINX.md), Part 2**. It adds PMS as one more site on the nginx
dentglitz uses, with a backup first and a config test before anything goes live. Use the
two ports from Step 2.

**Expected:** `nginx -t` says `test is successful`, and the reload leaves dentglitz
running as before. If the test fails, nothing has changed; NGINX.md says how to back out
and what each error means.

### Step 13: Check everything

Run the checks in **[NGINX.md](NGINX.md), Part 3**. They cover HTTPS, the redirect from
HTTP, the API, the live queue's WebSocket, and dentglitz.

Then check the containers are all still up:

```bash
cd ~/project/pms-multi-tenant-api && docker compose ps
cd ~/project/pms-multi-tenant-web && docker compose ps
docker ps --format '{{.Names}}\t{{.Status}}' | grep dentglitz   # same as before PMS
```

Then in a browser:

1. Open `https://admin.pms.astraleinfo.com` and sign in with the Step 10 account.
2. Create a clinic, for example slug `demo`.
3. Open `https://demo.pms.astraleinfo.com`. You should see that clinic's sign-in page.
4. Invite a user. If the email arrives, SMTP works.

**PMS is live.**

---

## Part B: Day-to-day

### Deploy an update

```bash
cd ~/project/pms-multi-tenant-api
git pull
APP_RELEASE=$(git rev-parse --short HEAD) docker compose up -d --build

cd ~/project/pms-multi-tenant-web
git pull
REVISION=$(git rev-parse HEAD) docker compose up -d --build
```

Migrations run automatically before the new API starts.

### Nightly database backup

Add this to `crontab -e` (it's one line):

```bash
0 2 * * * mkdir -p /root/backups/pms && docker exec pms-multi-tenant-api-db-1 pg_dump -U postgres -Fc pms > /root/backups/pms/pms-$(date +\%F).dump && find /root/backups/pms -name '*.dump' -mtime +14 -delete
```

This keeps 14 days of backups. Also copy them off the server regularly: a backup on the
same disk is lost along with the server.

Restore a backup (this **replaces** the current data):

```bash
cd ~/project/pms-multi-tenant-api
docker compose stop api worker beat flower
docker exec -i pms-multi-tenant-api-db-1 pg_restore -U postgres -d pms --clean --if-exists < /root/backups/pms/pms-YYYY-MM-DD.dump
docker compose start api worker beat flower
```

### Logs

```bash
cd ~/project/pms-multi-tenant-api
docker compose logs -f api              # or: worker, beat, flower, db, migrate
cd ~/project/pms-multi-tenant-web
docker compose logs -f web
tail -f /var/log/nginx/pms.access.log /var/log/nginx/pms.error.log
```

Changing the nginx site, renewing the certificate, or switching PMS off at the edge:
[NGINX.md, Changing things later](NGINX.md#changing-things-later).

### Restart

```bash
cd ~/project/pms-multi-tenant-api && docker compose restart api worker beat flower
cd ~/project/pms-multi-tenant-web && docker compose restart web
```

### Flower (background jobs)

Sign in at `https://admin.pms.astraleinfo.com` as super admin and open the jobs section.
Flower is deliberately never exposed on its own port.

### Take PMS offline (dentglitz unaffected)

```bash
rm /etc/nginx/sites-enabled/pms.conf && nginx -t && systemctl reload nginx
cd ~/project/pms-multi-tenant-web && docker compose down
cd ~/project/pms-multi-tenant-api && docker compose down     # data stays in the volumes
```

Never add `-v` to `docker compose down` in production: it deletes the database.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `required variable … is missing a value` | A value is missing from `.env` | API: add it (Step 7). Web: `NEXT_PUBLIC_APP_DOMAIN` (Step 11) |
| `pms.astraleinfo.com` shows a clinic page or "unclaimed" | Web built without `NEXT_PUBLIC_APP_DOMAIN` | Step 11, then `docker compose up -d --build` |
| `migrate` shows `Exited (1)` | Database not ready, or a wrong password | `docker compose logs migrate`; if the passwords are wrong on a fresh install, redo Step 8's reset |
| `api` shows `unhealthy` or keeps restarting | A production check failed | `docker compose logs api` names the setting, e.g. `MAIL_PROVIDER` or `PUBLIC_SCHEME` |
| Browser shows a certificate error, `nginx -t` fails, or the WebSocket check fails | nginx or the certificate | [NGINX.md, Troubleshooting](NGINX.md#troubleshooting) |
| `502 Bad Gateway` on a PMS address | The API or web container is down | `docker compose ps` in both repos; start what's down |
| Clinic address doesn't load at all | `*.pms` DNS record missing | Step 4 |
| Invitation emails don't arrive | SMTP settings | Check the `SMTP_*` values; `docker compose logs worker` |
| Changed a password in `.env`, database still uses the old one | It's only read on the first start | Change it in Postgres with `ALTER ROLE`, or reset the empty volume (Step 8) |

# nginx setup for PMS

How PMS is put behind the nginx that already runs on the server and serves
`dentglitz.com`. This guide covers the certificate, the PMS site and how to check it.
For the rest of the deployment, see [DEPLOY.md](DEPLOY.md).

## What changes on the server

| Added | Purpose |
|---|---|
| `/etc/nginx/ssl/pms.astraleinfo.com/fullchain.pem`, `privkey.pem` | Wildcard certificate for `pms.astraleinfo.com` and `*.pms.astraleinfo.com` |
| `/etc/nginx/sites-available/pms.conf` | The PMS site, generated from `deploy/host-nginx/pms.conf.template` |
| `/etc/nginx/sites-enabled/pms.conf` | A link to it, which is what switches it on |
| `/var/log/nginx/pms.access.log`, `pms.error.log` | PMS logs, kept apart from dentglitz's |
| `~/.acme.sh/` and one cron line | Issues the certificate and renews it |

Nothing else changes. `nginx.conf`, the `dentglitz.com` site and the `default` site stay
as they are. The PMS site only answers for `pms.astraleinfo.com` and its subdomains.
Requests for any other name still go wherever they go today: nginx loads `sites-enabled`
in alphabetical order, so `pms.conf` comes after `default` and `dentglitz.com` and can't
become the fallback site by accident.

The PMS site doesn't turn on HTTP/2. On nginx before 1.25, HTTP/2 is switched on for a
whole port at once, so enabling it for PMS would switch `dentglitz.com` over too. If
`nginx -v` shows 1.25.1 or later, you can enable it for PMS alone by adding `http2 on;`
to the `listen 443` server block in the template.

## What the PMS site does

| Request | Goes to |
|---|---|
| `http://*.pms.astraleinfo.com/...` | Redirected (308) to the same address on `https://` |
| `https://…/api/v1/ws/...` | The API, as a WebSocket (live queue); idle sockets kept 5 minutes |
| `https://…/api/v1/auth/...` | The API, with a tighter rate limit (sign-in, refresh, password reset) |
| `https://…/api/health…` | Blocked (404): health checks are for the server, not the internet |
| `https://…/api/...` | The API on `127.0.0.1:18000`, with `/api` removed from the path |
| `https://…/` (everything else) | The web app on `127.0.0.1:13000` |

It also:
- passes the original address (`Host`) through unchanged, because the subdomain is how
  the API knows which clinic a request is for
- caps uploads at 20 MB
- rate-limits each IP address (20 requests a second with bursts; 60 sign-ins a minute)
- sends `Strict-Transport-Security`
- logs in JSON without query strings, because the live queue's sign-in token travels in one

Every shared name in the file (`pms_api`, `pms_rl_api`, `pms_json`, …) starts with
`pms_`, so it can't clash with anything the dentglitz config defines.

Run every command below as `root`.

---

## Before you start

All four must be true:

```bash
# 1. DNS points at this server (all three print the server's IP)
dig +short pms.astraleinfo.com admin.pms.astraleinfo.com test.pms.astraleinfo.com

# 2. nginx is running (prints "active") and its version is 1.18 or later
systemctl is-active nginx
nginx -v

# 3. envsubst is installed (prints a path; if not: apt install -y gettext-base)
command -v envsubst

# 4. The PMS containers answer on their ports (DEPLOY.md Steps 9 and 11)
curl -s 127.0.0.1:18000/health; echo          # {"status":"ok"}
curl -sI 127.0.0.1:13000/ | head -1           # HTTP/1.1 200 OK
```

For Part 1 (the certificate), only check 1 is needed, so you can do it before the
containers exist. Parts 2 and 3 need all four.

The ports are `PMS_API_PORT` in the API `.env` and `PMS_WEB_PORT` in the web `.env`
(DEPLOY.md Step 2). This guide uses the defaults, 18000 and 13000. If you changed them,
use your numbers everywhere below.

---

## Part 1: The certificate

One wildcard certificate covers `pms.astraleinfo.com` and every
`*.pms.astraleinfo.com`, including `admin.` and every clinic created later. A wildcard
can only be issued by proving you control the DNS. acme.sh does that through the
Netlify API, so it can also renew without you.

**1. Create a Netlify token.** Netlify → **User settings** → **Applications** →
**Personal access tokens** → **New access token**. Copy it.

**2. Install acme.sh and issue the certificate:**

```bash
curl -s https://get.acme.sh | sh -s email=astraleinfo@gmail.com
source ~/.bashrc

export NETLIFY_ACCESS_TOKEN='<paste the token>'

~/.acme.sh/acme.sh --issue --server letsencrypt --dns dns_netlify \
  -d pms.astraleinfo.com -d '*.pms.astraleinfo.com'
```

**Expected:** after a minute or two, `Cert success.` acme.sh saves the token for
renewals, so you don't need to export it again.

**3. Install it where nginx will read it:**

```bash
mkdir -p /etc/nginx/ssl/pms.astraleinfo.com
~/.acme.sh/acme.sh --install-cert -d pms.astraleinfo.com \
  --key-file       /etc/nginx/ssl/pms.astraleinfo.com/privkey.pem \
  --fullchain-file /etc/nginx/ssl/pms.astraleinfo.com/fullchain.pem \
  --reloadcmd      "nginx -t && systemctl reload nginx"
```

The reload it runs is harmless: nginx doesn't use this certificate until Part 2, so
dentglitz carries on as before. On every renewal it reloads nginx again, but only after
`nginx -t` passes.

**4. Check it:**

```bash
openssl x509 -in /etc/nginx/ssl/pms.astraleinfo.com/fullchain.pem -noout -ext subjectAltName -enddate
crontab -l | grep acme.sh
```

**Expected:**
- `DNS:pms.astraleinfo.com, DNS:*.pms.astraleinfo.com`
- `notAfter=` about 90 days from today
- a cron line for acme.sh, which checks daily and renews about 30 days before expiry

---

## Part 2: Add the PMS site

**1. Back up the current nginx config:**

```bash
cp -a /etc/nginx /root/nginx-backup-$(date +%F)
```

**2. Generate the site from the template:**

```bash
cd ~/project/pms-multi-tenant-api
APP_DOMAIN=pms.astraleinfo.com PMS_API_PORT=18000 PMS_WEB_PORT=13000 \
  envsubst '${APP_DOMAIN} ${PMS_API_PORT} ${PMS_WEB_PORT}' \
  < deploy/host-nginx/pms.conf.template > /etc/nginx/sites-available/pms.conf
```

The list in quotes after `envsubst` matters: it fills in only those three values and
leaves nginx's own variables (`$host`, `$remote_addr`, …) alone.

**3. Check what it generated:**

```bash
grep -nE 'server_name|server 127.0.0.1|ssl_certificate ' /etc/nginx/sites-available/pms.conf
```

**Expected:**

```
server_name .pms.astraleinfo.com;
server_name .pms.astraleinfo.com;
ssl_certificate     /etc/nginx/ssl/pms.astraleinfo.com/fullchain.pem;
server 127.0.0.1:18000 max_fails=0;
server 127.0.0.1:13000 max_fails=0;
```

If a port or the domain is blank, a value was missing from the command in step 2. Run it
again.

**4. Switch it on, test, reload:**

```bash
ln -s /etc/nginx/sites-available/pms.conf /etc/nginx/sites-enabled/pms.conf
nginx -t && systemctl reload nginx
```

**Expected:** `syntax is ok` and `test is successful`, with no warnings from `pms.conf`.

`nginx -t` checks the whole config before anything goes live. `&&` means the reload only
happens if the test passes, and a reload swaps the config without dropping any open
connection, dentglitz's included.

**If `nginx -t` fails,** nothing has changed: nginx is still running the old config.
Switch the site off again, then look the error up under
[Troubleshooting](#troubleshooting):

```bash
rm /etc/nginx/sites-enabled/pms.conf
```

---

## Part 3: Check it

```bash
# PMS over HTTPS
curl -sI https://pms.astraleinfo.com       | head -1     # HTTP/1.1 200
curl -sI https://admin.pms.astraleinfo.com | head -1     # HTTP/1.1 307 (on to /platform)
curl -sI http://pms.astraleinfo.com        | head -1     # HTTP/1.1 308 (redirect to https)

# The API through nginx: 422 = reached (the empty sign-in was rejected by the API itself)
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' \
  https://admin.pms.astraleinfo.com/api/v1/platform/auth/login

# Health checks stay private: 404
curl -s -o /dev/null -w '%{http_code}\n' https://pms.astraleinfo.com/api/health

# The live queue's WebSocket
curl --http1.1 -s -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  'https://admin.pms.astraleinfo.com/api/v1/ws/queue?access_token=test'

# dentglitz is unaffected (same result as before PMS)
curl -sI https://dentglitz.com | head -1
```

**Expected** for the WebSocket check: `403`. The API received the socket and refused the
fake token, which is correct. `404` means the upgrade didn't get through, and `502` means
the API is down.

Then open `https://admin.pms.astraleinfo.com` in a browser. The padlock should show a
valid certificate and the console sign-in page should load.

---

## Changing things later

### After the template changes in git, or to change a port

Generate, check and reload exactly as in Part 2. The link in `sites-enabled` already
exists, so skip `ln`:

```bash
cd ~/project/pms-multi-tenant-api && git pull
cp /etc/nginx/sites-available/pms.conf /root/pms.conf.bak
APP_DOMAIN=pms.astraleinfo.com PMS_API_PORT=18000 PMS_WEB_PORT=13000 \
  envsubst '${APP_DOMAIN} ${PMS_API_PORT} ${PMS_WEB_PORT}' \
  < deploy/host-nginx/pms.conf.template > /etc/nginx/sites-available/pms.conf
nginx -t && systemctl reload nginx
```

If `nginx -t` fails, put the previous version back:

```bash
cp /root/pms.conf.bak /etc/nginx/sites-available/pms.conf && nginx -t && systemctl reload nginx
```

When you change a port, change it in the `.env` as well and recreate that container
(`docker compose up -d`), so both sides agree.

### Certificate renewal

Nothing to do: the acme.sh cron job renews it and reloads nginx. To check:

```bash
~/.acme.sh/acme.sh --list                     # shows the renewal date
~/.acme.sh/acme.sh --renew -d pms.astraleinfo.com --force    # renew now, if ever needed
```

If the Netlify token is revoked, renewals fail. Create a new token and issue again
(Part 1, step 2).

### Logs

```bash
tail -f /var/log/nginx/pms.access.log      # one JSON line per request
tail -f /var/log/nginx/pms.error.log
```

### Switch PMS off at the edge (dentglitz unaffected)

```bash
rm /etc/nginx/sites-enabled/pms.conf
nginx -t && systemctl reload nginx
```

The PMS containers keep running but can't be reached from outside. To switch it back on,
create the link again (Part 2, step 4).

### Last resort: restore the whole nginx config

Only if nginx itself is broken and removing the PMS link didn't fix it. Use the backup from
Part 2:

```bash
mv /etc/nginx /root/nginx-broken-$(date +%F-%H%M)
cp -a /root/nginx-backup-YYYY-MM-DD /etc/nginx
nginx -t && systemctl reload nginx
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `nginx -t`: `cannot load certificate "/etc/nginx/ssl/pms.astraleinfo.com/fullchain.pem"` | Part 1 not done, or installed elsewhere | Part 1, step 3 |
| `nginx -t`: `invalid port in upstream "127.0.0.1:"` | A port was missing from the `envsubst` command | Part 2, step 2 with both ports |
| `nginx -t`: `invalid port in upstream "127.0.0.1:${PMS_API_PORT}"` | The template was copied without `envsubst` | Part 2, step 2 |
| `nginx -t`: `invalid number of arguments in "map" directive` | `envsubst` ran without the quoted list, so it blanked nginx's own `$` variables too | Part 2, step 2, copied exactly |
| `nginx -t`: `server name "." is invalid` | `APP_DOMAIN` was missing from the `envsubst` command | Part 2, step 2 |
| `nginx -t`: `limit_req_zone "pms_rl_api" is already bound` | The site is enabled twice (e.g. a copy in `conf.d/` or a second link) | `grep -rl pms_rl_api /etc/nginx/`; keep only `sites-available/pms.conf` and its one link |
| A PMS address shows another site's page | Another site names that exact host; an exact name beats PMS's `.pms.astraleinfo.com` | `grep -rn "pms.astraleinfo.com" /etc/nginx/sites-enabled/` and remove it from the other site |
| `envsubst: command not found` | Package missing | `apt install -y gettext-base` |
| `502 Bad Gateway` | The API or web container isn't answering on its port | `curl 127.0.0.1:18000/health` and `curl -I 127.0.0.1:13000/`; start what's down (DEPLOY.md) |
| `502` on `/api/...` only, but `/health` works locally | The port in `pms.conf` doesn't match `PMS_API_PORT` | Compare `grep 'server 127' /etc/nginx/sites-available/pms.conf` with both `.env` files |
| Browser: certificate is for another name, or not trusted | nginx picked another site's certificate: the PMS site isn't enabled, or DNS points elsewhere | Check the `sites-enabled` link and the `dig` results in "Before you start" |
| WebSocket check returns `404` | The upgrade isn't reaching the API | Make sure the site came from the current template; regenerate it ("Changing things later") |
| `429 Too Many Requests` | The per-IP rate limit was hit | Normal under abuse. If a whole clinic shares one IP and hits it, raise `rate=` in the template's `limit_req_zone` lines and regenerate |
| `413 Request Entity Too Large` | Upload over 20 MB | Raise `client_max_body_size` in the template and regenerate |
| Renewal failed (acme.sh log, or the certificate expired) | Netlify token revoked or DNS moved | New token, then Part 1, step 2 |

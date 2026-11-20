#!/usr/bin/env bash
# LAYAK Odoo 19 -> 20 migration on the VPS.
#
#   bash vps_migrate.sh preflight   # read-only checks, prints what it found
#   bash vps_migrate.sh stage       # build the separate Odoo 20 stack from this bundle (no downtime)
#   bash vps_migrate.sh sync        # copy everything new from the LIVE Odoo 19 into Odoo 20 (read-only on 19, repeatable)
#   bash vps_migrate.sh status
#   CONFIRM_CUTOVER=yes-andrew-approved bash vps_migrate.sh cutover   # final sync + switch Caddy to Odoo 20
#   CONFIRM_ROLLBACK=yes bash vps_migrate.sh rollback                 # switch Caddy back to Odoo 19
#
# Every name below can be overridden with an environment variable. Nothing here deletes data:
# Odoo 19 containers, volumes and database are never removed.
set -euo pipefail

BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DBNAME="${DBNAME:-layak_db}"
PROD_ODOO="${PROD_ODOO:-odoo}"                 # live Odoo 19 container
PROD_DB="${PROD_DB:-odoo-db}"                  # live postgres 15 container
PROD_CONF="${PROD_CONF:-/root/stack/odoo/odoo.conf}"
CADDY="${CADDY:-caddy}"
NEW_DIR="${NEW_DIR:-/root/stack/odoo20}"
NEW_PROJECT="${NEW_PROJECT:-layak-odoo20}"
NEW_WEB="${NEW_WEB:-odoo20}"                   # new Odoo 20 container (Caddy upstream after cutover)
NEW_DB="${NEW_DB:-odoo20-db}"                  # new postgres 16 container
IMG_ODOO="${IMG_ODOO:-odoo:20.0-20261112}"
IMG_PG="${IMG_PG:-postgres:16}"
EDGE_NET="${EDGE_NET:-}"                       # docker network shared with Caddy (auto-detected from $PROD_ODOO)
PUBLIC_URL="${PUBLIC_URL:-https://www.layak.or.id}"
SKIP_CADDY="${SKIP_CADDY:-0}"                  # rehearsal only
SKIP_PUBLIC_CHECK="${SKIP_PUBLIC_CHECK:-0}"    # rehearsal only
XFER="$BUNDLE/xfer"
LOGS="$BUNDLE/logs"
TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGS" "$XFER"

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   OK  %s\n' "$*"; }
warn() { printf '   !!  %s\n' "$*"; }
die()  { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }
# host path for docker -v (Git Bash on Windows needs a Windows path; Linux uses it as is)
hp()   { if pwd -W >/dev/null 2>&1; then (cd "$1" && pwd -W); else (cd "$1" && pwd); fi; }
# local file path for docker cp (same idea)
lp()   { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
export MSYS_NO_PATHCONV=1

running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" = "true" ]; }
prod_pguser() { docker exec "$PROD_DB" printenv POSTGRES_USER 2>/dev/null || echo odoo; }
psql_prod() { docker exec -i "$PROD_DB" psql -U "$(prod_pguser)" -d "$DBNAME" -qAt -v ON_ERROR_STOP=1 "$@"; }
psql_new()  { docker exec -i "$NEW_DB" psql -U odoo -d "$DBNAME" -qAt -v ON_ERROR_STOP=1 "$@"; }
compose()   { (cd "$NEW_DIR" && docker compose "$@"); }
detect_edge() {
  [ -n "$EDGE_NET" ] && return
  # the network that BOTH the live Odoo and Caddy are on (Caddy must reach the new container there)
  local nets_odoo nets_caddy n
  nets_odoo="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PROD_ODOO")"
  nets_caddy="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CADDY" 2>/dev/null || true)"
  for n in $nets_odoo; do case " $nets_caddy " in *" $n "*) EDGE_NET="$n"; break ;; esac; done
  [ -n "$EDGE_NET" ] || die "no docker network shared by $PROD_ODOO and $CADDY; set EDGE_NET"
  [ "$(echo "$nets_odoo" | wc -w)" -gt 1 ] && warn "$PROD_ODOO is on several networks ($nets_odoo); using the one shared with $CADDY: $EDGE_NET" || true
}
http_status_inside() {  # $1 container, $2 path -> HTTP status from inside the container
  docker exec "$1" python3 -c "
import urllib.request, urllib.error, sys
try: print(urllib.request.urlopen('http://localhost:8069$2', timeout=30).status)
except urllib.error.HTTPError as e: print(e.code)
except Exception as e: print('ERR', type(e).__name__)"
}

# --------------------------------------------------------------------------------------------- preflight
preflight() {
  say "Bundle"
  (cd "$BUNDLE" && sha256sum -c SHA256SUMS --quiet) && ok "checksums match" || die "bundle checksum mismatch"
  say "Live Odoo 19"
  running "$PROD_ODOO" && ok "$PROD_ODOO running ($(docker inspect -f '{{.Config.Image}}' "$PROD_ODOO"))" || die "$PROD_ODOO not running"
  running "$PROD_DB" && ok "$PROD_DB running ($(docker inspect -f '{{.Config.Image}}' "$PROD_DB"))" || die "$PROD_DB not running"
  psql_prod -c "select 1" >/dev/null && ok "database $DBNAME reachable"
  ok "base module: $(psql_prod -c "select latest_version from ir_module_module where name='base'")"
  ok "posted entries: $(psql_prod -c "select count(*) from account_move where state='posted'"), last change: $(psql_prod -c "select max(write_date) from account_move")"
  [ -f "$PROD_CONF" ] && ok "config file $PROD_CONF found" || warn "config $PROD_CONF not found (set PROD_CONF); admin password will be random"
  docker exec "$PROD_ODOO" sh -c 'test -n "${HOST:-}"' && ok "$PROD_ODOO has HOST env for the DB" || warn "no HOST env in $PROD_ODOO: exports use db_host from its config"
  detect_edge; ok "shared network with Caddy: $EDGE_NET"
  say "Caddy"
  if running "$CADDY"; then
    local cf; cf="$(caddyfile_host_path || true)"
    ok "$CADDY running; Caddyfile on host: ${cf:-not found}"
    [ -n "$cf" ] && grep -nE "\b${PROD_ODOO}:[0-9]+" "$cf" | sed 's/^/     /' || true
  else warn "$CADDY not running"; fi
  say "New stack"
  for c in "$NEW_WEB" "$NEW_DB"; do docker inspect "$c" >/dev/null 2>&1 && warn "container $c already exists" || ok "container name $c free"; done
  [ -e "$NEW_DIR" ] && warn "$NEW_DIR exists" || ok "$NEW_DIR free"
  df -h "$(dirname "$NEW_DIR")" | tail -1 | awk '{print "   OK  free disk: " $4}'
}

caddyfile_host_path() {
  docker inspect -f '{{range .Mounts}}{{.Destination}}|{{.Source}}{{"\n"}}{{end}}' "$CADDY" | awk -F'|' '
    $1=="/etc/caddy/Caddyfile" {print $2; exit}
    $1=="/etc/caddy" {print $2 "/Caddyfile"; exit}'
}

# --------------------------------------------------------------------------------------------- stage
stage() {
  detect_edge
  [ -e "$NEW_DIR/docker-compose.yml" ] && die "$NEW_DIR already staged (remove it yourself only if you are sure)"
  say "Files in $NEW_DIR"
  mkdir -p "$NEW_DIR/config" "$NEW_DIR/addons-layak" "$NEW_DIR/addons-om"
  tar xzf "$BUNDLE/addons/layak_wa_registration_181bfa4.tar.gz" -C "$NEW_DIR/addons-layak"
  tar xzf "$BUNDLE/addons/odoomates_20.0.tar.gz" -C "$NEW_DIR/addons-om"
  ok "addons: $(ls "$NEW_DIR/addons-layak" | tr '\n' ' ')+ $(ls -d "$NEW_DIR"/addons-om/*/ | wc -l) Odoo Mates modules"
  local pgpass; pgpass="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-32)"
  printf 'PGPASS=%s\n' "$pgpass" > "$NEW_DIR/.env"; chmod 600 "$NEW_DIR/.env"
  {
    echo "[options]"
    if [ -f "$PROD_CONF" ] && grep -qE '^\s*admin_passwd\s*=' "$PROD_CONF"; then grep -m1 -E '^\s*admin_passwd\s*=' "$PROD_CONF"
    else echo "admin_passwd = $(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"; fi
    cat <<EOF
db_host = $NEW_DB
db_port = 5432
db_user = odoo
db_password = $pgpass
dbfilter = ^${DBNAME}\$
list_db = False
proxy_mode = True
workers = 2
http_interface = 0.0.0.0
addons_path = /mnt/layak-addons,/mnt/om-addons,/usr/lib/python3/dist-packages/odoo/addons
EOF
  } > "$NEW_DIR/config/odoo.conf"
  chmod 644 "$NEW_DIR/config/odoo.conf"
  ok "odoo.conf written (admin password taken from the Odoo 19 config, not printed)"
  cat > "$NEW_DIR/docker-compose.yml" <<EOF
# LAYAK Odoo 20 (built by vps_migrate.sh from the local migration bundle).
name: $NEW_PROJECT
services:
  db:
    image: $IMG_PG
    container_name: $NEW_DB
    restart: unless-stopped
    environment:
      - POSTGRES_DB=postgres
      - POSTGRES_USER=odoo
      - POSTGRES_PASSWORD=\${PGPASS}
    volumes:
      - db-data:/var/lib/postgresql/data
  web:
    image: $IMG_ODOO
    container_name: $NEW_WEB
    restart: unless-stopped
    depends_on: [db]
    volumes:
      - ./config:/etc/odoo
      - ./addons-layak:/mnt/layak-addons:ro
      - ./addons-om:/mnt/om-addons:ro
      - web-data:/var/lib/odoo
    networks: [default, edge]
networks:
  edge:
    external: true
    name: $EDGE_NET
volumes:
  db-data:
  web-data:
EOF
  ok "docker-compose.yml written (no public ports; joins network $EDGE_NET for Caddy)"

  say "Database"
  compose pull -q
  compose up -d db
  for i in $(seq 1 60); do docker exec "$NEW_DB" pg_isready -U odoo >/dev/null 2>&1 && break; sleep 1; done
  docker exec "$NEW_DB" psql -U odoo -d postgres -qAtc "select 1 from pg_database where datname='$DBNAME'" | grep -q 1 \
    && die "database $DBNAME already exists in $NEW_DB"
  docker exec "$NEW_DB" createdb -U odoo -O odoo "$DBNAME"
  docker exec -i "$NEW_DB" pg_restore -U odoo -d "$DBNAME" --no-owner --role=odoo --exit-on-error < "$BUNDLE/data/layak20.dump"
  ok "restored local Odoo 20 database as $DBNAME"

  say "Filestore"
  compose run --rm --no-deps -T -u root -v "$(hp "$BUNDLE/data"):/bundle:ro" --entrypoint sh web -c "
    set -e; d=/var/lib/odoo/.local/share/Odoo/filestore; mkdir -p \$d; cd \$d
    test ! -e '$DBNAME' || { echo 'filestore exists'; exit 1; }
    tar xzf /bundle/filestore_layak20.tar.gz && mv layak20 '$DBNAME' && chown -R odoo:odoo /var/lib/odoo
    echo \$(find '$DBNAME' -type f | wc -l) files"
  ok "filestore restored"

  enforce_safe_mode
  say "Start Odoo 20 (not public yet)"
  compose up -d web
  for i in $(seq 1 90); do [ "$(http_status_inside "$NEW_WEB" /web/login)" = "200" ] && break; sleep 2; done
  [ "$(http_status_inside "$NEW_WEB" /web/login)" = "200" ] && ok "Odoo 20 answers on $NEW_WEB:8069" || die "Odoo 20 does not answer; see: docker logs $NEW_WEB"
  echo; echo "Next: bash $0 sync"
}

enforce_safe_mode() {  # before cutover Odoo 20 must not message customers or run the bot
  psql_new -c "update ir_config_parameter set value='1' where key='layak_school.wa.dry_run'" \
           -c "update ir_cron set active=false where id in (select res_id from ir_model_data where module='layak_wa_registration' and model='ir.cron')" \
           -c "update ir_mail_server set active=false" >/dev/null
  ok "safe mode: WhatsApp dry-run on, bot jobs paused, mail servers off"
}

# --------------------------------------------------------------------------------------------- sync
export_19() {  # $1 script, $2 output name ; runs read-only inside the live Odoo 19 container
  local out="/tmp/layak_etl_$2"
  docker exec "$PROD_ODOO" mkdir -p /tmp/layak-etl
  docker cp "$(lp "$BUNDLE/etl/$1")" "$PROD_ODOO:/tmp/layak-etl/$1" >/dev/null
  docker exec -i -e ETL_OUT="$out" "$PROD_ODOO" sh -c '
    set -- ; A="-c /etc/odoo/odoo.conf"
    [ -n "${HOST:-}" ] && A="$A --db_host=$HOST"; [ -n "${PORT:-}" ] && A="$A --db_port=$PORT"
    [ -n "${USER:-}" ] && [ "$USER" != "root" ] && A="$A --db_user=$USER"; [ -n "${PASSWORD:-}" ] && A="$A --db_password=$PASSWORD"
    odoo shell $A -d '"$DBNAME"' --no-http --log-level=warn < /tmp/layak-etl/'"$1" > "$LOGS/${TS}_export_$2.log" 2>&1 || true
  grep -E "^EXPORT[A-Z_]*_OK" "$LOGS/${TS}_export_$2.log" | sed 's/^/   /' || die "export $1 failed, see $LOGS/${TS}_export_$2.log"
  docker cp "$PROD_ODOO:$out" "$(lp "$XFER/$2")" >/dev/null
  docker exec "$PROD_ODOO" rm -f "$out"
}

import_20() {  # $1 script ; prints the result lines
  compose run --rm --no-deps -T -v "$(hp "$BUNDLE/etl"):/etl:ro" -v "$(hp "$XFER"):/xfer:ro" web \
    sh -c "odoo shell -c /etc/odoo/odoo.conf -d $DBNAME --no-http < /etl/$1" > "$LOGS/${TS}_import_${1%.py}.log" 2>&1 || true
  grep -E "^(LOG|RESULT|COUNTS|DIFF|NAMEDIFF|PROBLEM|FAILED|BALANCE|ROLLED|COMMITTED)" "$LOGS/${TS}_import_${1%.py}.log" | sed 's/^/   /' | head -60
  grep -q "^COMMITTED" "$LOGS/${TS}_import_${1%.py}.log" || die "import $1 did not commit, see $LOGS/${TS}_import_${1%.py}.log"
}

sync() {
  running "$NEW_WEB" || die "Odoo 20 stack not running (run stage first)"
  say "Export from live Odoo 19 (read-only)"
  export_19 export_mirror19.py export.json
  export_19 export_wa_mirror19.py export_wa.json
  export_19 export_apps_mirror19.py export_apps.json
  export_19 export_website_mirror19.py export_website.json
  export_19 export_config_mirror19.py export_config.json
  say "Import into Odoo 20"
  echo " accounting:"; import_20 import_odoo20.py
  echo " registrations:"; import_20 import_wa_odoo20.py
  echo " projects/sales/mailing/notes:"; import_20 import_apps_odoo20.py
  echo " website:"; import_20 import_website_odoo20.py
  echo " configuration:"; import_20 import_config_odoo20.py
  [ "${IN_CUTOVER:-0}" = "1" ] || enforce_safe_mode
  compose restart web >/dev/null
  ok "sync finished; logs in $LOGS"
}

# --------------------------------------------------------------------------------------------- cutover
copy_secrets() {  # live 19 -> 20, values never printed or written to disk
  say "Copy live settings and credentials (database to database)"
  psql_prod -c "\copy (select key, value from ir_config_parameter where key like 'layak\_school.wa.%' or key = 'web.base.url') to stdout" \
   | psql_new -c "create temp table s(key varchar, value text)" -c "\copy s from stdin" \
       -c "insert into ir_config_parameter(key, value, create_uid, write_uid, create_date, write_date) select key, value, 1, 1, now(), now() from s on conflict (key) do update set value = excluded.value, write_date = now()" \
       -c "select 'parameters', count(*) from s" | sed 's/^/   /'
  psql_prod -c "\copy (select login, password from res_users where active and password is not null) to stdout" \
   | psql_new -c "create temp table s(login varchar, password varchar)" -c "\copy s from stdin" \
       -c "update res_users u set password = s.password from s where u.login = s.login" \
       -c "select 'passwords', count(*) from res_users u join s on s.login = u.login" | sed 's/^/   /'
  psql_prod -c "\copy (select k.name, u.login, k.scope, k.expiration_date, k.index, k.key, k.create_date from res_users_apikeys k join res_users u on u.id = k.user_id) to stdout" \
   | psql_new -c "create temp table s(name varchar, login varchar, scope varchar, expiration_date timestamp, index varchar, key varchar, create_date timestamp)" -c "\copy s from stdin" \
       -c "insert into res_users_apikeys(name, user_id, scope, expiration_date, index, key, create_date) select s.name, u.id, s.scope, s.expiration_date, s.index, s.key, s.create_date from s join res_users u on u.login = s.login where not exists (select 1 from res_users_apikeys k where k.index = s.index and k.user_id = u.id)" \
       -c "select 'api keys', count(*) from res_users_apikeys" | sed 's/^/   /'
  local cols="name, from_filter, smtp_host, smtp_port, smtp_authentication, smtp_encryption, smtp_user, smtp_pass, smtp_ssl_certificate, smtp_ssl_private_key, smtp_debug, sequence, active, google_gmail_access_token, google_gmail_access_token_expiration, google_gmail_refresh_token, microsoft_outlook_access_token, microsoft_outlook_access_token_expiration, microsoft_outlook_refresh_token, owner_limit_count, owner_limit_time"
  psql_prod -c "\copy (select $cols from ir_mail_server) to stdout" \
   | psql_new -c "create temp table s as select $cols from ir_mail_server limit 0" -c "\copy s from stdin" \
       -c "insert into ir_mail_server($cols, create_uid, write_uid, create_date, write_date) select $cols, 1, 1, now(), now() from s where not exists (select 1 from ir_mail_server m where m.name = s.name)" \
       -c "update ir_mail_server m set active = s.active from s where m.name = s.name" \
       -c "select 'mail servers', count(*) from ir_mail_server" | sed 's/^/   /'
  psql_prod -c "\copy (select d.name, c.active from ir_cron c join ir_model_data d on d.model = 'ir.cron' and d.res_id = c.id and d.module = 'layak_wa_registration') to stdout" \
   | psql_new -c "create temp table s(name varchar, active boolean)" -c "\copy s from stdin" \
       -c "update ir_cron c set active = s.active from s, ir_model_data d where d.model = 'ir.cron' and d.module = 'layak_wa_registration' and d.name = s.name and d.res_id = c.id" \
       -c "select 'bot jobs active', count(*) filter (where active) from s" | sed 's/^/   /'
  psql_prod -c "\copy (select coalesce(domain, '') from website where id = 1) to stdout" \
   | psql_new -c "create temp table s(domain varchar)" -c "\copy s from stdin" \
       -c "update website w set domain = nullif(s.domain, '') from s where w.id = 1" -c "select 'website domain set', count(*) from website where id = 1 and domain is not null" | sed 's/^/   /'
}

cutover() {
  [ "${CONFIRM_CUTOVER:-}" = "yes-andrew-approved" ] || die "cutover needs Andrew's go: CONFIRM_CUTOVER=yes-andrew-approved"
  running "$NEW_WEB" && running "$PROD_ODOO" || die "both $NEW_WEB and $PROD_ODOO must be running"
  mkdir -p "$BUNDLE/cutover"
  say "Backup of live Odoo 19 before cutover"
  docker exec "$PROD_DB" pg_dump -U "$(prod_pguser)" -Fc -d "$DBNAME" > "$BUNDLE/cutover/prod19_${TS}.dump"
  ok "$(du -h "$BUNDLE/cutover/prod19_${TS}.dump" | cut -f1) -> cutover/prod19_${TS}.dump"
  local cf=""
  if [ "$SKIP_CADDY" != "1" ]; then
    cf="$(caddyfile_host_path)"; [ -f "$cf" ] || die "Caddyfile not found"
    cp -p "$cf" "$BUNDLE/cutover/Caddyfile.before_${TS}"; ok "Caddyfile backed up"
    grep -qE "\b${PROD_ODOO}:(8069|8072)\b" "$cf" || die "Caddyfile has no ${PROD_ODOO}:8069 upstream; adjust by hand"
  fi
  say "Final sync"
  IN_CUTOVER=1 sync
  copy_secrets
  compose restart web >/dev/null
  for i in $(seq 1 60); do [ "$(http_status_inside "$NEW_WEB" /web/login)" = "200" ] && break; sleep 2; done
  say "Stop Odoo 19 (container and data are kept)"
  docker stop "$PROD_ODOO" >/dev/null && ok "$PROD_ODOO stopped"
  if [ "$SKIP_CADDY" != "1" ]; then
    say "Switch Caddy to Odoo 20"
    sed -E -i "s/\b${PROD_ODOO}:(8069|8072)\b/${NEW_WEB}:\1/g" "$cf"
    diff <(grep -nE "\b(${PROD_ODOO}|${NEW_WEB}):[0-9]+" "$BUNDLE/cutover/Caddyfile.before_${TS}") <(grep -nE "\b(${PROD_ODOO}|${NEW_WEB}):[0-9]+" "$cf") | sed 's/^/   /' || true
    docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
      || { cp -p "$BUNDLE/cutover/Caddyfile.before_${TS}" "$cf"; docker start "$PROD_ODOO"; die "new Caddyfile invalid; restored and restarted Odoo 19"; }
    docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile && ok "Caddy reloaded"
  fi
  say "Checks"
  if [ "$SKIP_PUBLIC_CHECK" != "1" ]; then
    for p in /web/login / /contactus /about-us /layak/wa/meta; do printf '   %-14s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL$p")"; done
    echo "   expected: 200 200 200 200 403"
  else
    for p in /web/login / /contactus /layak/wa/meta; do printf '   %-14s %s\n' "$p" "$(http_status_inside "$NEW_WEB" "$p")"; done
  fi
  cat <<EOF

Cutover done. Rollback if anything is wrong:  CONFIRM_ROLLBACK=yes bash $0 rollback
Manual follow-ups for Andrew (see VPS_MIGRATION.md):
  - Accounting > Configuration > Bank Accounts: mark both Mandiri accounts as Trusted
  - n8n: update journal/account IDs (LF 7 -> 11, LC 8 -> 12, accounts: id_mapping_accounts.txt), then re-enable the bank import
  - Log in and post one test entry; check the website
EOF
}

rollback() {
  [ "${CONFIRM_ROLLBACK:-}" = "yes" ] || die "set CONFIRM_ROLLBACK=yes"
  local last; last="$(ls -1t "$BUNDLE"/cutover/Caddyfile.before_* 2>/dev/null | head -1 || true)"
  if [ "$SKIP_CADDY" != "1" ]; then
    [ -n "$last" ] || die "no Caddyfile backup found"
    cp -p "$last" "$(caddyfile_host_path)"
    docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile && ok "Caddy back on Odoo 19 ($last)"
  fi
  docker start "$PROD_ODOO" >/dev/null && ok "$PROD_ODOO started"
  docker stop "$NEW_WEB" >/dev/null && ok "$NEW_WEB stopped (data kept)"
  warn "anything entered in Odoo 20 after the cutover is NOT in Odoo 19"
}

status() {
  docker ps -a --filter "name=^/(${PROD_ODOO}|${PROD_DB}|${NEW_WEB}|${NEW_DB}|${CADDY})$" --format '   {{.Names}}\t{{.Image}}\t{{.Status}}'
  running "$NEW_WEB" && echo "   Odoo 20 /web/login: $(http_status_inside "$NEW_WEB" /web/login)" || true
  running "$NEW_DB" && echo "   Odoo 20 posted entries: $(psql_new -c "select count(*) from account_move where state='posted'")" || true
}

case "${1:-}" in
  preflight|stage|sync|cutover|rollback|status) "$1" ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac

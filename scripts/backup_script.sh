#!/usr/bin/env bash
# Read-only export of the LAYAK Odoo 19 production data for a local mirror.
# Run on the VPS:  bash ~/backup_script.sh
# It only reads from the running containers; it does not stop or change them.
set -euo pipefail

ODOO=odoo          # Odoo container
DB=odoo-db         # Postgres container
DBNAME=layak_db
OUT=~/layak-mirror/$(date +%F_%H%M)

DOCKER=docker
if ! docker ps >/dev/null 2>&1; then DOCKER="sudo docker"; fi

mkdir -p "$OUT" && chmod 700 ~/layak-mirror "$OUT"
cd "$OUT"
echo "Exporting to $OUT"

# 1. Sanity checks
$DOCKER exec $DB sh -c 'psql -U "$POSTGRES_USER" -d postgres -Atc "select datname from pg_database"' | grep -qx "$DBNAME" \
  || { echo "ERROR: database $DBNAME not found"; exit 1; }

# 2. Database dump (custom format). Uses the container's own POSTGRES_USER.
echo "[1/4] Dumping database $DBNAME ..."
$DOCKER exec $DB sh -c "pg_dump -U \"\$POSTGRES_USER\" -Fc -d $DBNAME" > layak_db_vps.dump

# 3. Filestore (attachments)
echo "[2/4] Archiving filestore ..."
FS=$($DOCKER exec $ODOO sh -c "for d in /var/lib/odoo/filestore /var/lib/odoo/.local/share/Odoo/filestore; do [ -d \"\$d/$DBNAME\" ] && echo \$d && break; done")
[ -n "$FS" ] || { echo "ERROR: filestore for $DBNAME not found"; exit 1; }
$DOCKER exec $ODOO tar czf - -C "$FS" "$DBNAME" > filestore_vps.tar.gz

# 4. Addons and odoo.conf (read from inside the container, whatever the host path is)
echo "[3/4] Archiving addons and odoo.conf ..."
$DOCKER exec $ODOO tar czf - -C /mnt/extra-addons . > addons_vps.tar.gz
$DOCKER exec $ODOO cat /etc/odoo/odoo.conf > odoo_vps.conf

# 5. Info file for the mirror (versions, addons path, installed custom modules)
echo "[4/4] Writing info ..."
{
  echo "exported_at: $(date -Is)"
  echo "odoo_image: $($DOCKER inspect $ODOO --format '{{.Config.Image}} {{.Image}}')"
  echo "db_image:   $($DOCKER inspect $DB --format '{{.Config.Image}} {{.Image}}')"
  echo "filestore_path: $FS"
  echo "addons_path: $(grep -E '^\s*addons_path' odoo_vps.conf || echo 'not set in conf')"
  echo "mounts:"; $DOCKER inspect $ODOO --format '{{range .Mounts}}  {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
  echo "latest entries:"
  $DOCKER exec $DB sh -c "psql -U \"\$POSTGRES_USER\" -d $DBNAME -Atc \"select 'last move date', max(date) from account_move where state='posted'\""
  echo "installed non-core modules:"
  $DOCKER exec $DB sh -c "psql -U \"\$POSTGRES_USER\" -d $DBNAME -Atc \"select name||' '||coalesce(latest_version,'') from ir_module_module where state='installed' and coalesce(author,'') not ilike '%odoo s.a.%' order by 1\""
} > export_info.txt

sha256sum layak_db_vps.dump filestore_vps.tar.gz addons_vps.tar.gz odoo_vps.conf > SHA256SUMS
chmod 600 ./*

echo
ls -lh
echo
cat export_info.txt
echo
echo "DONE. Download the folder $OUT to the laptop, then delete it here:  rm -rf ~/layak-mirror"

# Odoo 19 -> 20 accounting ETL

Copies the chart of accounts, journals, analytic plans/accounts, referenced partners and all
posted + draft journal entries (original numbers kept) from the Odoo 19 mirror into the Odoo 20 database.
Cancelled entries are skipped. Exchange files go to `backups/etl/` (gitignored; contains financial data).

Run from the repo root in Git Bash.

```bash
# 1. Export from the mirror (read-only)
cd stacks/mirror19
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T -u root -e HOME=/var/lib/odoo \
  -v "$(cd ../../scripts/etl && pwd -W):/etl:ro" -v "$(cd ../../backups/etl && pwd -W):/xfer" \
  web sh -c 'odoo shell -c /etc/odoo/odoo.conf -d layak_db --no-http < /etl/export_mirror19.py' | grep EXPORT_OK

# 2. Snapshot the target, then import
cd ../odoo20
docker compose exec -T db pg_dump -U odoo -Fc -d layak20 > ../../backups/etl/layak20_before_import.dump
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T \
  -v "$(cd ../../scripts/etl && pwd -W):/etl:ro" -v "$(cd ../../backups/etl && pwd -W):/xfer:ro" \
  web sh -c 'odoo shell -c /etc/odoo/odoo.conf -d layak20 --no-http < /etl/import_odoo20.py' \
  | grep -E '^(LOG|RESULT|DIFF|NAMEDIFF|BALANCE|ROLLED|COMMITTED)'
```

The import:
- matches accounts and journals by code, creates the missing ones, and makes names/types follow the source;
- tags every record it creates with an external id `__etl_mirror19__.<kind>_<source id>`, so re-running only adds new entries;
- compares posted balances per account per month and the entry numbers/states with the source,
  and **commits only when there are zero differences** (otherwise it rolls back and prints `DIFF` lines).

For a final cut-over: take a fresh VPS export (`scripts/backup_script.sh`), restore it into the mirror,
and run steps 1-2 again. Only entries new since the last run are added. Entries that were changed
in the source after an earlier import are not updated; the control totals will flag them.

## LAYAK School registrations (layak_wa_registration)

Needs the module installed in `layak20` first. Then:

```bash
# export from the mirror (users, contacts, class products, WA records, proof images)
cd stacks/mirror19
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T -u root -e HOME=/var/lib/odoo \
  -v "$(cd ../../scripts/etl && pwd -W):/etl:ro" -v "$(cd ../../backups/etl && pwd -W):/xfer" \
  web sh -c 'odoo shell -c /etc/odoo/odoo.conf -d layak_db --no-http < /etl/export_wa_mirror19.py' | grep EXPORT_WA_OK

# copy the layak_school.wa.* system parameters DB-to-DB (values never printed or written to a file),
# then force dry-run + Midtrans sandbox on the test copy
docker compose exec -T db psql -U odoo -d layak_db -qAt \
  -c "\copy (select key, value from ir_config_parameter where key like 'layak\_school.wa.%') to stdout" \
 | docker compose -f ../odoo20/docker-compose.yml exec -T db psql -U odoo -d layak20 -qAt -v ON_ERROR_STOP=1 \
  -c "create temp table src_param(key varchar, value text)" -c "\copy src_param from stdin" \
  -c "insert into ir_config_parameter(key, value, create_uid, write_uid, create_date, write_date) select key, value, 1, 1, now(), now() from src_param on conflict (key) do update set value=excluded.value, write_date=now()" \
  -c "update ir_config_parameter set value='1' where key='layak_school.wa.dry_run'" \
  -c "update ir_config_parameter set value='sandbox' where key='layak_school.wa.midtrans_env'"

# import (commits only if every record maps)
cd ../odoo20
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T \
  -v "$(cd ../../scripts/etl && pwd -W):/etl:ro" -v "$(cd ../../backups/etl && pwd -W):/xfer:ro" \
  web sh -c 'odoo shell -c /etc/odoo/odoo.conf -d layak20 --no-http < /etl/import_wa_odoo20.py' \
  | grep -E '^(LOG|COUNTS|PROBLEM|ROLLED|COMMITTED)'
```

Users are created without passwords. Account payments from the WA flow are not migrated (the 10 on the VPS were tests).

## Projects, sales, categories, mailing lists, chatter notes

`export_apps_mirror19.py` -> `import_apps_odoo20.py` (run after the accounting and WA imports; it reuses their mappings).
Onboarding "Welcome ...!" to-dos without a project are not migrated.

## Website (website 1) and company details

`export_website_mirror19.py` -> `import_website_odoo20.py`. Applies the theme (Odoo 20's own theme_cobalt views),
copies website-specific views with all translations, pages, menus (incl. mega menu), images (ids rewritten),
custom SCSS palette and company details/logo. `PATCHES` in the import holds the known 19 -> 20 template changes.

## Approved reclassifications

`backups/etl/reclass_applied.json` (rules: entry + source account -> target account) is read by `import_odoo20.py`:
rules are applied to the source data before import, to already-imported entries in place (with a chatter note),
and to the expected control totals, so the zero-difference check still holds. The VPS is not changed by this.

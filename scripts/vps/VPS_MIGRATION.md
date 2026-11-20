# LAYAK Odoo 19 -> 20 on the VPS: handoff for Claude on the VPS

Written 2026-11-20 by Claude on Andrew's laptop, where the whole migration was built and tested.
Owner: **Andrew (COO, LAYAK)**. Reply in English, be concise, **ask Andrew before anything destructive or public**.

## What this is

On the laptop, a complete Odoo 20 copy of production was built and checked against a copy of this VPS's
Odoo 19 (exported 2026-11-20 02:16 UTC). This bundle contains that Odoo 20 database and a script that:

1. builds a **separate** Odoo 20 + postgres 16 stack next to the live one (`stage`, no downtime),
2. copies everything entered in the **live Odoo 19** since then into Odoo 20 (`sync`, read-only on 19, repeatable),
3. on Andrew's go, does a final sync, copies live credentials/settings, stops Odoo 19 and points Caddy at Odoo 20 (`cutover`),
4. can point Caddy back to Odoo 19 (`rollback`).

The whole sequence (preflight, stage, sync with new data, cutover, rollback) was rehearsed on the laptop
against a copy of production. Only the Caddy edit and the public URL checks could not be rehearsed there.

## Rules

- Do **not** touch DNS, n8n, real credentials, or the Odoo 19 containers/volumes/database (stopping the Odoo 19
  web container during cutover is the only change to it). Never delete anything of Odoo 19.
- Never print or copy out the values of system parameters (`ir_config_parameter`), passwords, API keys,
  SMTP settings or `odoo.conf` secrets. Listing keys/names is fine. The script copies them database-to-database.
- Do not run `odoo neutralize` on any production database.
- `cutover` only after Andrew explicitly says go. `rollback` only if Andrew agrees (or the site is down).
- Do not push or change any git repository.
- This bundle contains the database (financial data, password hashes, API-key hashes, bot tokens).
  Keep it under /root and close the bundle folder itself (`chmod 700` on the folder, `chmod -R go-rwx data cutover`),
  but leave `etl/` and `xfer/` readable: Odoo inside the containers runs as a non-root user and must read them.
  Delete `data/`, `xfer/` and `cutover/` when Andrew confirms the migration is done.

## Bundle contents

| Path | What |
|---|---|
| `vps_migrate.sh` | the migration script (phases above) |
| `data/layak20.dump` | Odoo 20 database (pg_dump custom format), restored as `layak_db` |
| `data/filestore_layak20.tar.gz` | its attachments (renamed to `layak_db` on restore) |
| `addons/layak_wa_registration_1b8135c.tar.gz` | LAYAK School registration module ported to Odoo 20 (addons repo, branch `migration/20.0`, commit 1b8135c) |
| `addons/odoomates_20.0.tar.gz` | Odoo Mates accounting modules, 20.0 branch, commit ac70c59 (21 modules, incl. new dependencies) |
| `etl/` | export (run inside the live Odoo 19 container) and import (run in Odoo 20) scripts, with README |
| `xfer/reclass_applied.json` | 39 account reclassifications Andrew approved; re-applied on every sync |
| `id_mapping_accounts.txt` | old (19) -> new (20) account ids, for n8n |
| `SHA256SUMS` | checksums (preflight verifies them) |

Defaults in the script match what the laptop saw on this VPS: containers `odoo`, `odoo-db`, `caddy`,
config `/root/stack/odoo/odoo.conf`, database `layak_db`, new stack in `/root/stack/odoo20` with containers
`odoo20` and `odoo20-db`, image `odoo:20.0-20261112` + `postgres:16`. Override with environment variables if needed
(see the top of the script). The new stack keeps the database name `layak_db`, so n8n's database setting stays valid.

## Steps

1. Put the bundle in `/root/layak-migration/` and run `bash vps_migrate.sh preflight`. Report the output to Andrew.
   Check especially: the shared docker network it detected (Caddy must reach `odoo20` on it), the Caddyfile lines that
   point at `odoo:8069` / `odoo:8072`, and free disk.
2. `bash vps_migrate.sh stage`. Builds the stack, restores the database and files, starts Odoo 20 in **safe mode**
   (WhatsApp dry-run on, bot jobs paused, no mail servers). Nothing is public; Caddy is unchanged.
3. `bash vps_migrate.sh sync`. Every import must end in `COMMITTED`. The accounting import must show
   `total diffs=0 | name/state diffs=0`. Run it again as often as you like; it only adds what is new.
4. Let Andrew look at Odoo 20 privately before cutover, through an SSH tunnel (nothing public):
   `docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' odoo20`, then on his laptop
   `ssh -L 8072:<that ip>:8069 root@<vps>` and open http://localhost:8072 (login with his normal account).
5. **Cutover** (Andrew's go, short freeze):
   - Andrew tells staff to stop entering data and **pauses the n8n bank-import workflow himself** (do not touch n8n).
   - `CONFIRM_CUTOVER=yes-andrew-approved bash vps_migrate.sh cutover`
   - It backs up the live 19 database (`cutover/prod19_*.dump`) and the Caddyfile, runs the final sync, copies the live
     bot settings (incl. dry-run/test-numbers/Midtrans as they are on 19), password hashes, API keys, the mail server,
     the bot jobs' on/off state and the website domain, stops the `odoo` container, rewrites `odoo:8069|8072` to
     `odoo20:8069|8072` in the Caddyfile, validates and reloads Caddy, then checks the public URLs
     (expected `200 200 200 200 403`).
6. Follow-ups for Andrew (the script prints them): mark both Mandiri bank accounts **Trusted**
   (Accounting > Configuration > Bank Accounts; Odoo 20 does not allow scripts to do it), update n8n ids and
   re-enable the bank import, post one test entry, check the website and the WhatsApp bot.

## If something does not match

- **Accounting import `ROLLED_BACK` with `DIFF` lines**: an entry that already existed on 2026-11-20 was changed in
  Odoo 19 afterwards (the import only adds new entries, it never edits migrated ones). Find them with
  `select name, write_date from account_move where write_date > '2026-11-20 02:16' and create_date <= '2026-11-20 02:16'`
  on the live database, show Andrew, and agree how to bring the change into Odoo 20. Do not bypass the check.
- **`NAMEDIFF`**: an entry changed state or number (for example the two LF drafts of 31 May 2026,
  LF/2026/05/0053 and /0054, which Andrew decided to keep as drafts). Same approach: report, agree, then fix.
- **Website import `FAILED`**: 5 `website_sale_wishlist.*` header views are expected to fail (wishlists were merged
  into eCommerce in Odoo 20). Anything else: report it.
- **Odoo 20 does not start** after `stage`: `docker logs odoo20`. Nothing public is affected.
- **After cutover the site misbehaves**: `CONFIRM_ROLLBACK=yes bash vps_migrate.sh rollback` puts Caddy back on Odoo 19
  and starts it. Anything entered in Odoo 20 in between is not in Odoo 19; tell Andrew.

## Decisions already made (do not redo)

- OpenUpgrade 20.0 had no migration scripts, so the books were rebuilt in a fresh Odoo 20 with full history
  (1,920 posted + 2 draft entries) and checked per account per month; analytic totals match.
- Audit figures at 31 Oct 2026: LC 43,479,335.07 matches. LF shows 107,378,264.22; the audited 109,878,264.22
  includes the two drafts above (2,500,000), which stay drafts.
- 39 reclassifications approved by Andrew (yearly net result unchanged); 85 unused/duplicate accounts and 4 empty
  journals archived; renames: 21100010 Trade Payable, 11210020 Employee Advances; one cash account (11110020 Cash in Hand).
- All users on time zone Asia/Jakarta. Midtrans stays **sandbox** (verification pending).
- The 10 November WhatsApp test payments and the "Test Budget" were not migrated; Odoo "Welcome" to-dos were skipped.

## n8n id changes (for Andrew)

Journals: LF 7 -> 11, LC 8 -> 12, MID 16 -> 13, SCH 15 -> 14, INV1 17 -> 15, INV 9 -> 1, BILL 10 -> 2, CSH1 14 -> 8, MISC 3 -> 3.
Accounts: `id_mapping_accounts.txt` (columns: kind | old id | new id | code | name). Accounts created on the VPS after
2026-11-20 get new ids; list all with:
`select replace(d.name,'account_',''), d.res_id, a.code_store->>'1' from ir_model_data d join account_account a on a.id=d.res_id where d.module='__etl_mirror19__' and d.model='account.account' order by 3`
(in `odoo20-db`). Codes did not change, so n8n steps that use account codes keep working.

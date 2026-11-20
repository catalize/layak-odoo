# Import the mirror export (export_mirror19.py) into the Odoo 20 database.
# Run inside the odoo20 web container via `odoo shell`, see scripts/etl/README.md.
# Idempotent: every created record gets an external id __etl_mirror19__.<kind>_<source id>,
# so a re-run skips what already exists. Commits ONLY if all control totals match.
import json
import os
from collections import defaultdict

SRC = os.environ.get('ETL_IN', '/xfer/export.json')
MOD = '__etl_mirror19__'
data = json.load(open(SRC, encoding='utf-8'))

# Approved reclassifications (optional): move name + source account -> target account.
# Applied to the source data before import, and to already-imported entries in place (step 5b).
RECLASS_FILE = os.environ.get('ETL_RECLASS', '/xfer/reclass_applied.json')
RECLASS = json.load(open(RECLASS_FILE, encoding='utf-8'))['rules'] if os.path.exists(RECLASS_FILE) else []
_rules = {(r['move'], r['from']): r for r in RECLASS}
_hits = set()
for _m in data['moves']:
    for _l in _m['lines']:
        r = _rules.get((_m['name'], _l['account_code']))
        if r:
            _hits.add((r['move'], r['from']))
            if _m['state'] == 'posted':
                bal = round(_l['debit'] - _l['credit'], 2)
                month = _m['date'][:7]
                for code, sign in ((r['from'], -1), (r['to'], 1)):
                    k = '%s|%s' % (code, month)
                    data['control_totals'][k] = round(data['control_totals'].get(k, 0) + sign * bal, 2)
            _l['account_code'] = r['to']
_unused_rules = sorted(set(_rules) - _hits)
ctx = dict(tracking_disable=True, mail_create_nolog=True, mail_notrack=True)
IMD = env['ir.model.data']
log = []


def xid_get(kind, src_id, model):
    rec = IMD.search([('module', '=', MOD), ('name', '=', '%s_%s' % (kind, src_id))], limit=1)
    return env[model].browse(rec.res_id) if rec else None


def xid_set(kind, src_id, record):
    IMD.create({'module': MOD, 'name': '%s_%s' % (kind, src_id), 'model': record._name,
                'res_id': record.id, 'noupdate': True})


# 0. Feature settings the source has on (shows the Analytic column on journal items, etc.).
_settings = {'group_analytic_accounting': True, 'group_sale_delivery_address': True}
_cfg = env['res.config.settings']
_todo = {k: v for k, v in _settings.items() if k in _cfg._fields}
if _todo:
    _cfg.create(_todo).execute()
    log.append('settings enabled: %s' % ', '.join(sorted(_todo)))

# 1. Accounts: match by code, else create. Names/types follow the source.
Account = env['account.account'].with_context(active_test=False, **ctx)
acc_by_code = {}
for a in data['accounts']:
    linked = xid_get('account', a['id'], 'account.account')
    acc = linked or Account.search([('code', '=', a['code'])], limit=1)
    if linked:
        pass  # already linked by an earlier run: keep local renames/archiving decisions
    elif not acc:
        acc = Account.create({'code': a['code'], 'name': a['name'], 'account_type': a['account_type'],
                              'reconcile': a['reconcile']})
        log.append('account created %s %s' % (a['code'], a['name']))
    else:
        vals = {}
        if acc.name != a['name']:
            vals['name'] = a['name']
        if acc.account_type != a['account_type']:
            vals['account_type'] = a['account_type']
            log.append('account type changed %s: %s -> %s' % (a['code'], acc.account_type, a['account_type']))
        if acc.reconcile != a['reconcile']:
            vals['reconcile'] = a['reconcile']
        if vals:
            acc.write(vals)
    if not IMD.search_count([('module', '=', MOD), ('name', '=', 'account_%s' % a['id'])]):
        xid_set('account', a['id'], acc)
    acc_by_code[a['code']] = acc

# 2. Journals: match by code, else create.
Journal = env['account.journal'].with_context(active_test=False, **ctx)
jr_by_code = {}
for j in data['journals']:
    jr = xid_get('journal', j['id'], 'account.journal') or Journal.search([('code', '=', j['code'])], limit=1)
    if not jr:
        vals = {'code': j['code'], 'name': j['name'], 'type': j['type']}
        if j['default_account_code']:
            vals['default_account_id'] = acc_by_code[j['default_account_code']].id
        jr = Journal.create(vals)
        log.append('journal created %s %s (%s)' % (j['code'], j['name'], j['type']))
    elif j['default_account_code'] and jr.default_account_id != acc_by_code[j['default_account_code']]:
        log.append('journal %s default account differs: %s vs source %s' % (
            j['code'], jr.default_account_id.code, j['default_account_code']))
    if jr.name != j['name'] and not IMD.search_count([('module', '=', MOD), ('name', '=', 'journal_%s' % j['id'])]):
        jr.name = j['name']
    if not IMD.search_count([('module', '=', MOD), ('name', '=', 'journal_%s' % j['id'])]):
        xid_set('journal', j['id'], jr)
    jr_by_code[j['code']] = jr

# 3. Analytic plans and accounts.
plan_map, an_map = {}, {}
for p in data['analytic_plans']:
    plan = xid_get('plan', p['id'], 'account.analytic.plan') or env['account.analytic.plan'].search([('name', '=', p['name'])], limit=1)
    if not plan:
        plan = env['account.analytic.plan'].create({'name': p['name']})
        xid_set('plan', p['id'], plan)
        log.append('analytic plan created %s' % p['name'])
    plan_map[p['id']] = plan
for a in data['analytic_accounts']:
    an = xid_get('analytic', a['id'], 'account.analytic.account')
    if not an:
        an = env['account.analytic.account'].create({'name': a['name'], 'code': a['code'],
                                                     'plan_id': plan_map[a['plan_id']].id, 'active': a['active']})
        xid_set('analytic', a['id'], an)
    an_map[str(a['id'])] = an.id


def remap_distribution(dist):
    if not dist:
        return False
    return {','.join(str(an_map[k]) for k in key.split(',')): pct for key, pct in dist.items()}


# 4. Partners (only those referenced).
partner_map = {}
for p in data['partners']:
    par = xid_get('partner', p['id'], 'res.partner')
    if not par:
        par = env['res.partner'].with_context(**ctx).create({k: p[k] for k in ('name', 'is_company', 'email', 'phone')})
        xid_set('partner', p['id'], par)
    partner_map[p['id']] = par.id

# 5. Journal entries, in date/name order, original numbers kept.
Move = env['account.move'].with_context(**ctx)
created = skipped = 0
to_post = Move.browse()
for m in data['moves']:
    if xid_get('move', m['id'], 'account.move'):
        skipped += 1
        continue
    move = Move.create({
        'move_type': 'entry', 'journal_id': jr_by_code[m['journal_code']].id, 'date': m['date'],
        'name': m['name'], 'ref': m['ref'], 'narration': m['narration'],
        'line_ids': [(0, 0, {
            'account_id': acc_by_code[l['account_code']].id, 'name': l['name'],
            'debit': l['debit'], 'credit': l['credit'],
            'partner_id': partner_map.get(l['partner_id']) if l['partner_id'] else False,
            'analytic_distribution': remap_distribution(l['analytic_distribution']),
            'date_maturity': l['date_maturity'],
        }) for l in m['lines']],
    })
    xid_set('move', m['id'], move)
    if m['state'] == 'posted':
        to_post |= move
    created += 1
    if created % 200 == 0:
        to_post._post(soft=False)
        to_post = Move.browse()
to_post._post(soft=False)
env.flush_all()

# 5b. Apply reclassifications to entries imported before the rule existed
reclassed = 0
for r in RECLASS:
    move = env['account.move'].search([('name', '=', r['move'])], limit=1)
    src_acc = Account.search([('code', '=', r['from'])], limit=1)
    dst_acc = Account.search([('code', '=', r['to'])], limit=1)
    if not move or not dst_acc:
        log.append('reclass %s: entry or target account missing' % r['move'])
        continue
    lines = move.line_ids.filtered(lambda l: l.account_id == src_acc)
    if not lines:
        continue
    was_posted = move.state == 'posted'
    if was_posted:
        move.button_draft()
    lines.with_context(**ctx).write({'account_id': dst_acc.id})
    if was_posted:
        move._post(soft=False)
    env['mail.message'].sudo().create({
        'model': 'account.move', 'res_id': move.id, 'message_type': 'comment', 'subtype_id': env.ref('mail.mt_note').id,
        'author_id': env.ref('base.partner_root').id,
        'body': 'Reclassified %s -> %s (%d line(s)), approved in reclass review 2026-11-20 %s.' % (
            r['from'], r['to'], len(lines), r.get('ref', '')),
    })
    reclassed += 1
if RECLASS:
    log.append('reclass rules=%d applied now=%d unused=%s' % (len(RECLASS), reclassed, _unused_rules or '-'))
env.flush_all()

# 6. Source accounts that were archived stay archived.
for a in data['accounts']:
    if not a['active'] and 'active' in Account._fields:
        acc_by_code[a['code']].active = False

# 7. Verify against the source control totals and the expected move names.
env.cr.execute("""
    select l.account_id, to_char(l.date, 'YYYY-MM'), sum(l.balance)
      from account_move_line l where l.parent_state = 'posted' group by 1, 2
""")
code_of = {acc.id: code for code, acc in acc_by_code.items()}
got = defaultdict(float)
for acc_id, month, bal in env.cr.fetchall():
    got['%s|%s' % (code_of.get(acc_id, 'UNMAPPED:%s' % acc_id), month)] += float(bal)
exp = data['control_totals']
diffs = sorted(k for k in set(exp) | set(got) if round(exp.get(k, 0) - got.get(k, 0), 2) != 0)

src_names = {(m['name'], m['state']) for m in data['moves']}
imported = env['account.move'].browse(
    IMD.search([('module', '=', MOD), ('model', '=', 'account.move')]).mapped('res_id'))
dst_names = {(m.name, m.state) for m in imported}
name_diffs = src_names ^ dst_names


def bank_balance(code):
    jr = jr_by_code[code]
    env.cr.execute("select coalesce(sum(balance),0) from account_move_line where account_id=%s and parent_state='posted' and date<='2026-10-31'",
                   [jr.default_account_id.id])
    return float(env.cr.fetchone()[0])


for line in log:
    print('LOG', line)
print('RESULT moves created=%d skipped=%d | total diffs=%d | name/state diffs=%d' % (
    created, skipped, len(diffs), len(name_diffs)))
for k in diffs[:20]:
    print('DIFF', k, 'source', exp.get(k, 0), 'odoo20', round(got.get(k, 0), 2))
for n in sorted(name_diffs)[:20]:
    print('NAMEDIFF', n)
print('BALANCE LF 31-Oct %.2f | LC 31-Oct %.2f' % (bank_balance('LF'), bank_balance('LC')))

if diffs or name_diffs:
    env.cr.rollback()
    print('ROLLED_BACK (nothing saved)')
else:
    env.cr.commit()
    print('COMMITTED')

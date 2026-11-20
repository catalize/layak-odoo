# Import remaining configuration (export_config_mirror19.py) into Odoo 20. Idempotent.
import json
import os

d = json.load(open(os.environ.get('ETL_IN', '/xfer/export_config.json'), encoding='utf-8'))
c = env.company
log = []

# Company bank accounts, linked to their journals
PB = env['res.partner.bank'].with_context(active_test=False)
NEW_API = 'account_number' in PB._fields  # Odoo 20: bank data is stored on the account itself
for b in d['banks']:
    if NEW_API:
        acc = PB.search([('account_number', '=', b['acc_number']), ('partner_id', '=', c.partner_id.id)], limit=1)
        vals = {'account_number': b['acc_number'], 'partner_id': c.partner_id.id, 'bank_name': b['bank_name'] or False,
                'bank_bic': b['bic'] or False, 'holder_name': b['holder'] or False}
        # 'Trusted' (allow_out_payment) is a security decision in Odoo 20: set it by hand in the UI.
        if not acc and 'clearing_label_id' in PB._fields:
            default = PB.default_get(['clearing_label_id']).get('clearing_label_id') or env['clearing.label'].search([], limit=1).id
            vals['clearing_label_id'] = default
    else:
        bank = env['res.bank'].search([('name', '=', b['bank_name'])], limit=1) or env['res.bank'].create({'name': b['bank_name']})
        acc = PB.search([('acc_number', '=', b['acc_number']), ('partner_id', '=', c.partner_id.id)], limit=1)
        vals = {'acc_number': b['acc_number'], 'partner_id': c.partner_id.id, 'bank_id': bank.id, 'acc_holder_name': b['holder']}
    if acc:
        vals = {k: v for k, v in vals.items() if acc[k] != (acc[k].browse(v) if hasattr(acc[k], 'browse') else v)}
        if vals:
            acc.write(vals)
    else:
        acc = PB.create(vals)
        log.append('bank account created %s (%s)' % (b['acc_number'], b['holder']))
    if NEW_API and b['allow_out_payment'] and not acc.allow_out_payment:
        log.append('ACTION: mark bank account %s as Trusted in Odoo (it is trusted on the VPS)' % b['acc_number'])
    for code in b['journals']:
        j = env['account.journal'].search([('code', '=', code)], limit=1)
        if j and j.bank_account_id != acc:
            j.bank_account_id = acc
            log.append('journal %s -> bank account %s' % (code, b['acc_number']))

# Document layout
L = d['layout']
vals = {}
if L['external_report_layout']:
    v = env.ref(L['external_report_layout'], raise_if_not_found=False)
    if v:
        vals['external_report_layout_id'] = v.id
    else:
        log.append('layout %s not in Odoo 20' % L['external_report_layout'])
for k in ('font', 'primary_color', 'secondary_color', 'layout_background'):
    if L.get(k) and k in c._fields:
        if c._fields[k].type == 'selection' and L[k] not in dict(c._fields[k]._description_selection(env)):
            log.append('%s=%s not valid in Odoo 20' % (k, L[k]))
            continue
        vals[k] = L[k]
if L.get('paperformat'):
    pf = env['report.paperformat'].search([('name', '=', L['paperformat'])], limit=1)
    if pf:
        vals['paperformat_id'] = pf.id
c.write(vals)
log.append('layout set: %s' % ', '.join('%s' % k for k in vals))

# User time zones and languages. FORCE_TZ (decided 2026-11-20): everyone works in Jakarta.
FORCE_TZ = os.environ.get('ETL_FORCE_TZ', 'Asia/Jakarta')
for u in d['users']:
    if FORCE_TZ:
        u['tz'] = FORCE_TZ
    user = env['res.users'].with_context(active_test=False).search([('login', '=', u['login'])], limit=1)
    if user:
        w = {}
        if u['tz'] and user.tz != u['tz']:
            w['tz'] = u['tz']
        if u['lang'] and user.lang != u['lang'] and env['res.lang']._lang_get(u['lang']):
            w['lang'] = u['lang']
        if w:
            user.write(w)
for line in log:
    print('LOG', line)
env.cr.commit()
print('COMMITTED')

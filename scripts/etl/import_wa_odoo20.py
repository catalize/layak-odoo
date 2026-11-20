# Import LAYAK School registration data (export_wa_mirror19.py) into Odoo 20.
# Requires layak_wa_registration installed in the target. Idempotent via external ids __etl_mirror19__.*.
# Users are created WITHOUT passwords (they cannot log in until a password is set).
# Commits only if every source record is mapped.
import base64
import json
import os

SRC = os.environ.get('ETL_IN', '/xfer/export_wa.json')
MOD = '__etl_mirror19__'
d = json.load(open(SRC, encoding='utf-8'))
ctx = dict(tracking_disable=True, mail_create_nolog=True, mail_notrack=True, no_reset_password=True, active_test=False)
IMD = env['ir.model.data']
log, problems = [], []
maps = {}  # model -> {source id: target id}


def kind(model):
    return model.replace('.', '_')


def xid_get(model, src_id):
    rec = IMD.search([('module', '=', MOD), ('name', '=', '%s_%s' % (kind(model), src_id))], limit=1)
    return rec.res_id if rec else None


def xid_set(model, src_id, res_id):
    if not xid_get(model, src_id):
        IMD.create({'module': MOD, 'name': '%s_%s' % (kind(model), src_id), 'model': model, 'res_id': res_id, 'noupdate': True})


def remember(model, src_id, res_id):
    maps.setdefault(model, {})[src_id] = res_id
    xid_set(model, src_id, res_id)


def convert(model, block, row):
    """Turn a source row into create/write vals, remapping relational ids."""
    vals = {}
    for n in block['fields']:
        if n not in env[model]._fields:
            continue
        v = row.get(n)
        rel = block['relations'].get(n)
        if rel:
            m = maps.get(rel, {})
            if isinstance(v, list):
                vals[n] = [(6, 0, [m[i] for i in v if i in m])]
                missing = [i for i in v if i not in m]
            else:
                vals[n] = m.get(v) if v else False
                missing = [v] if v and v not in m else []
            if missing:
                problems.append('%s %s: %s -> %s ids %s not mapped' % (model, row['id'], n, rel, missing))
        else:
            vals[n] = v
    return vals


# Existing records the source references by id
maps['res.company'] = {1: env.company.id}
maps['res.currency'] = {c.id: c.id for c in env['res.currency'].with_context(active_test=False).search([])}
country_by_code = {c.code: c.id for c in env['res.country'].search([])}
maps['res.country'] = {int(k): country_by_code[v] for k, v in d['countries'].items() if v in country_by_code}
maps['res.partner'] = {d['company_partner_id']: env.company.partner_id.id}

# 1. Users (no passwords) and their partners
Users = env['res.users'].with_context(**ctx)
for u in d['res.users']:
    user = Users.search([('login', '=', u['login'])], limit=1)
    if u['login'] == 'layak.insan.mandiri@gmail.com':
        user = env.ref('base.user_admin')  # the source admin (uid 2) is the target admin
    gids = [env.ref(x).id for x in u['group_xmlids'] if env.ref(x, raise_if_not_found=False)
            and env.ref(x)._name == 'res.groups']
    dropped = [x for x in u['group_xmlids'] if not env.ref(x, raise_if_not_found=False)]
    vals = {'name': u['name'], 'email': u['email'], 'wa_admin': u['wa_admin'], 'wa_super_admin': u['wa_super_admin'],
            'wa_phone': u['wa_phone']}
    if not user:
        vals.update(login=u['login'], group_ids=[(6, 0, gids)])
        user = Users.create(vals)
        log.append('user created %s (%d groups%s)' % (u['login'], len(gids), ', not in 20: %s' % dropped if dropped else ''))
    else:
        if u['login'] != 'layak.insan.mandiri@gmail.com':
            vals['group_ids'] = [(6, 0, gids)]
        user.write(vals)
    remember('res.users', u['id'], user.id)
    maps['res.partner'][u['partner_id']] = user.partner_id.id

# 2. Other partners (parents before children)
Partner = env['res.partner'].with_context(**ctx)
pb = d['res.partner']
for row in sorted(pb['rows'], key=lambda r: (r.get('parent_id') is not None, r['id'])):
    tid = xid_get('res.partner', row['id'])
    vals = convert('res.partner', pb, row)
    vals.pop('state_id', None)
    if not tid:
        tid = Partner.create(vals).id
    remember('res.partner', row['id'], tid)

# 3. Products: module products by xml id, others by name
Tmpl = env['product.template'].with_context(**ctx)
tb = d['product.template']
for row in tb['rows']:
    xml = d['product_xmlids'].get(str(row['id']))
    if xml and 'product_product_delivery' in xml:
        log.append('product skipped (delivery module not installed): %s' % row['name'])
        continue
    tid = xid_get('product.template', row['id'])
    if not tid and xml:
        rec = env.ref(xml, raise_if_not_found=False)
        tid = rec.id if rec else None
    if not tid:
        rec = Tmpl.search([('name', '=', row['name'])], limit=1)
        tid = rec.id if rec else None
    vals = convert('product.template', tb, row)
    if tid:
        Tmpl.browse(tid).write(vals)
    else:
        tid = Tmpl.create(vals).id
        log.append('product created %s' % row['name'])
    remember('product.template', row['id'], tid)

# 4. WA models in dependency order. Banks match by code; the rest are created.
order = ['layak.wa.bank', 'layak.wa.promo.group', 'layak.wa.promo', 'layak.wa.registration',
         'layak.wa.installment', 'layak.wa.paylink']
counts = {}
for model in order:
    block = d.get(model)
    if not block:
        continue
    M = env[model].with_context(**ctx)
    for row in block['rows']:
        tid = xid_get(model, row['id'])
        if not tid and model == 'layak.wa.bank' and row.get('code'):
            rec = M.search([('code', '=', row['code'])], limit=1)
            tid = rec.id if rec else None
        vals = convert(model, block, row)
        if tid:
            M.browse(tid).write(vals)
        else:
            tid = M.create(vals).id
        remember(model, row['id'], tid)
    counts[model] = len(block['rows'])

# 5. Proof attachments, then messages (they point to attachments)
maps['ir.attachment'] = {}
for a in d['ir.attachment']:
    tid = xid_get('ir.attachment', a['id'])
    if not tid:
        res_model = a['res_model']
        res_id = maps.get(res_model, {}).get(a['res_id']) if res_model else False
        tid = env['ir.attachment'].sudo().create({
            'name': a['name'], 'mimetype': a['mimetype'], 'raw': base64.b64decode(a['datas']) if a['datas'] else b'',
            'res_model': res_model if res_id or not res_model else False, 'res_id': res_id or 0,
        }).id
    remember('ir.attachment', a['id'], tid)

mb = d['layak.wa.message']
Msg = env['layak.wa.message'].with_context(**ctx)
for row in mb['rows']:
    tid = xid_get('layak.wa.message', row['id'])
    vals = convert('layak.wa.message', mb, row)
    if tid:
        Msg.browse(tid).write(vals)
    else:
        tid = Msg.create(vals).id
    remember('layak.wa.message', row['id'], tid)
counts['layak.wa.message'] = len(mb['rows'])

# Re-link attachments that belong to WA records created after them
for a in d['ir.attachment']:
    if a['res_model'] and a['res_model'].startswith('layak.wa.'):
        att = env['ir.attachment'].sudo().browse(maps['ir.attachment'][a['id']])
        rid = maps.get(a['res_model'], {}).get(a['res_id'])
        if rid and (att.res_model != a['res_model'] or att.res_id != rid):
            att.write({'res_model': a['res_model'], 'res_id': rid})

# Keep original creation timestamps (used for message ordering, reminders and reports)
for model in order + ['layak.wa.message']:
    block = d.get(model)
    if not block:
        continue
    table = env[model]._table
    for row in block['rows']:
        env.cr.execute('UPDATE "%s" SET create_date = %%s WHERE id = %%s' % table, [row['create_date'], maps[model][row['id']]])
env.invalidate_all()

# 6. Verify: every source record mapped, counts equal
for model, n in counts.items():
    got = env[model].with_context(active_test=False).search_count([('id', 'in', list(maps.get(model, {}).values()))])
    if got != n:
        problems.append('%s: source %d, target %d' % (model, n, got))
for line in log:
    print('LOG', line)
print('COUNTS', {m: (n, len(maps.get(m, {}))) for m, n in counts.items()},
      'users=%d partners=%d products=%d attachments=%d' % (
          len(d['res.users']), len(pb['rows']), len(maps.get('product.template', {})), len(maps['ir.attachment'])))
for p in problems[:30]:
    print('PROBLEM', p)
if problems:
    env.cr.rollback()
    print('ROLLED_BACK (%d problems)' % len(problems))
else:
    env.cr.commit()
    print('COMMITTED')

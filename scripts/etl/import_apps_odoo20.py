# Import projects, sales, product categories, mailing lists and chatter notes (export_apps_mirror19.py) into Odoo 20.
# Run after import_odoo20.py and import_wa_odoo20.py (reuses their id mappings). Idempotent; commits only without problems.
import base64
import json
import os

d = json.load(open(os.environ.get('ETL_IN', '/xfer/export_apps.json'), encoding='utf-8'))
wa = json.load(open(os.environ.get('ETL_WA', '/xfer/export_wa.json'), encoding='utf-8'))
MOD = '__etl_mirror19__'
ctx = dict(tracking_disable=True, mail_create_nolog=True, mail_notrack=True, active_test=False)
IMD = env['ir.model.data']
log, problems = [], []


def kind(model):
    return model.replace('.', '_')


LEGACY = {'account.move': 'move', 'account.account': 'account', 'account.journal': 'journal'}


def xid_get(model, src_id):
    names = ['%s_%s' % (kind(model), src_id)]
    if model in LEGACY:
        names.append('%s_%s' % (LEGACY[model], src_id))
    rec = IMD.search([('module', '=', MOD), ('name', 'in', names)], limit=1)
    return rec.res_id if rec else None


def xid_set(model, src_id, res_id):
    if not xid_get(model, src_id):
        IMD.create({'module': MOD, 'name': '%s_%s' % (kind(model), src_id), 'model': model, 'res_id': res_id, 'noupdate': True})


# Partner and user mapping from the earlier imports
users = {}
partners = {wa['company_partner_id']: env.company.partner_id.id}
for u in wa['res.users']:
    uid = xid_get('res.users', u['id'])
    if uid:
        users[u['id']] = uid
        partners[u['partner_id']] = env['res.users'].browse(uid).partner_id.id
for p in wa['res.partner']['rows']:
    pid = xid_get('res.partner', p['id'])
    if pid:
        partners[p['id']] = pid


def need(mapping, src, what):
    if not src:
        return False
    if src not in mapping:
        problems.append('%s %s not mapped' % (what, src))
        return False
    return mapping[src]


# 1. Product categories (match by full name, else create)
Cat = env['product.category'].with_context(**ctx)
cats = {}
for c in sorted(d['product.category'], key=lambda r: r['complete_name'].count('/')):
    cid = xid_get('product.category', c['id'])
    if not cid:
        rec = Cat.search([('complete_name', '=', c['complete_name'])], limit=1)
        if not rec:
            rec = Cat.create({'name': c['name'], 'parent_id': cats.get(c['parent_id']) or False})
            log.append('category created %s' % c['complete_name'])
        cid = rec.id
        xid_set('product.category', c['id'], cid)
    cats[c['id']] = cid
for tmpl_src, categ_src in d['product_categ'].items():
    tid = xid_get('product.template', int(tmpl_src))
    if tid and categ_src:
        env['product.template'].browse(tid).categ_id = cats[categ_src]

# 2. Projects, stages, tasks
Project, Stage, Task = (env[m].with_context(**ctx) for m in ('project.project', 'project.task.type', 'project.task'))
projects, stages, tasks = {}, {}, {}
for p in d['project.project']:
    pid = xid_get('project.project', p['id'])
    vals = {k: p[k] for k in ('name', 'description', 'privacy_visibility', 'date_start', 'date', 'active', 'sequence', 'color',
                             'label_tasks', 'allow_milestones') if k in p and k in Project._fields}
    vals.update(partner_id=need(partners, p.get('partner_id'), 'partner'), user_id=need(users, p.get('user_id'), 'user'))
    if pid:
        Project.browse(pid).write(vals)
    else:
        pid = Project.create(vals).id
        xid_set('project.project', p['id'], pid)
    projects[p['id']] = pid
for s in d['project.task.type']:
    sid = xid_get('project.task.type', s['id'])
    vals = {'name': s['name'], 'sequence': s['sequence'], 'fold': s['fold'],
            'project_ids': [(6, 0, [projects[x] for x in s['project_ids'] if x in projects])]}
    if sid:
        Stage.browse(sid).write(vals)
    else:
        sid = Stage.create(vals).id
        xid_set('project.task.type', s['id'], sid)
    stages[s['id']] = sid
for t in sorted(d['project.task'], key=lambda r: (r.get('parent_id') or 0, r['id'])):
    tid = xid_get('project.task', t['id'])
    vals = {k: t[k] for k in ('name', 'description', 'priority', 'date_deadline', 'state', 'sequence', 'allocated_hours',
                             'date_assign', 'date_end', 'color', 'active') if k in t and k in Task._fields}
    vals.update(project_id=projects[t['project_id']], stage_id=stages.get(t['stage_id']) or False,
                user_ids=[(6, 0, [users[u] for u in t.get('user_ids', []) if u in users])],
                partner_id=need(partners, t.get('partner_id'), 'partner'),
                parent_id=tasks.get(t.get('parent_id')) or False)
    if tid:
        Task.browse(tid).write(vals)
    else:
        tid = Task.create(vals).id
        xid_set('project.task', t['id'], tid)
    tasks[t['id']] = tid

# 3. Sales quotations
variant_of_tmpl = {}
for v_src, t_src in d['product_variant_tmpl'].items():
    tid = xid_get('product.template', t_src)
    if tid:
        variant_of_tmpl[int(v_src)] = env['product.template'].browse(tid).product_variant_id.id
Tax = env['account.tax'].with_context(active_test=False)


def map_tax(src_id):
    t = d['taxes'][str(src_id)]
    rec = Tax.search([('amount', '=', t['amount']), ('type_tax_use', '=', t['type_tax_use']),
                      ('amount_type', '=', t['amount_type']), ('name', '=', t['name'])], limit=1) or \
          Tax.search([('amount', '=', t['amount']), ('type_tax_use', '=', t['type_tax_use']),
                      ('amount_type', '=', t['amount_type'])], limit=1)
    if not rec:
        problems.append('tax %s (%s%%) has no match' % (t['name'], t['amount']))
    return rec.id


SO = env['sale.order'].with_context(**ctx)
orders = {}
for o in d['sale.order']:
    oid = xid_get('sale.order', o['id'])
    if not oid:
        lines = [l for l in d['sale.order.line'] if l['order_id'] == o['id']]
        tax_key = 'tax_ids' if lines and 'tax_ids' in lines[0] else 'tax_id'
        line_tax_field = 'tax_ids' if 'tax_ids' in env['sale.order.line']._fields else 'tax_id'
        so = SO.create({
            'name': o['name'], 'date_order': o['date_order'], 'validity_date': o.get('validity_date'),
            'partner_id': need(partners, o['partner_id'], 'partner'),
            'partner_invoice_id': need(partners, o.get('partner_invoice_id'), 'partner'),
            'partner_shipping_id': need(partners, o.get('partner_shipping_id'), 'partner'),
            'user_id': need(users, o.get('user_id'), 'user'), 'note': o.get('note'),
            'client_order_ref': o.get('client_order_ref'), 'origin': o.get('origin'),
            'order_line': [(0, 0, {
                'sequence': l['sequence'], 'display_type': l['display_type'] or False, 'name': l['name'],
                'product_id': need(variant_of_tmpl, l['product_id'], 'product') if l['product_id'] else False,
                'product_uom_qty': l['product_uom_qty'], 'price_unit': l['price_unit'], 'discount': l['discount'],
                line_tax_field: [(6, 0, [map_tax(x) for x in l.get(tax_key, [])])],
            }) for l in lines],
        })
        if o['state'] != 'draft':
            problems.append('sale order %s is %s; only drafts are handled' % (o['name'], o['state']))
        oid = so.id
        xid_set('sale.order', o['id'], oid)
    orders[o['id']] = oid

# 4. Mailing lists and contacts
lists = {}
for ml in d['mailing.list']:
    lid = xid_get('mailing.list', ml['id']) or env['mailing.list'].create({'name': ml['name'], 'is_public': ml.get('is_public', False)}).id
    xid_set('mailing.list', ml['id'], lid)
    lists[ml['id']] = lid
for c in d['mailing.contact']:
    if not xid_get('mailing.contact', c['id']):
        cid = env['mailing.contact'].create({'name': c['name'], 'email': c['email'], 'company_name': c.get('company_name'),
                                             'list_ids': [(6, 0, [lists[x] for x in c['list_ids'] if x in lists])]}).id
        xid_set('mailing.contact', c['id'], cid)

# 5. Attachments and chatter notes
res_maps = {'project.task': tasks, 'project.project': projects, 'sale.order': orders}


def target(model, res_id):
    if model in res_maps:
        return res_maps[model].get(res_id)
    if model == 'res.partner':
        return partners.get(res_id)
    return xid_get(model, res_id)


atts = {}
for a in d['ir.attachment']:
    aid = xid_get('ir.attachment', a['id'])
    if not aid:
        rid = target(a['res_model'], a['res_id']) if a['res_model'] else False
        vals = {'name': a['name'], 'mimetype': a['mimetype'], 'res_model': a['res_model'] if rid else False, 'res_id': rid or 0}
        if a['type'] == 'url':
            vals.update(type='url', url=a['url'])
        else:
            vals['raw'] = base64.b64decode(a['datas']) if a['datas'] else b''
        aid = env['ir.attachment'].sudo().create(vals).id
        xid_set('ir.attachment', a['id'], aid)
    atts[a['id']] = aid

note, comment = env.ref('mail.mt_note'), env.ref('mail.mt_comment')
for m in d['mail.message']:
    if xid_get('mail.message', m['id']):
        continue
    rid = target(m['model'], m['res_id'])
    if not rid:
        log.append('message %s skipped: %s %s not migrated' % (m['id'], m['model'], m['res_id']))
        continue
    mid = env['mail.message'].sudo().create({
        'model': m['model'], 'res_id': rid, 'body': m['body'], 'date': m['date'], 'message_type': 'comment',
        'subtype_id': (note if m['is_note'] else comment).id,
        'author_id': partners.get(m['author_id']) or env.ref('base.partner_root').id,
        'attachment_ids': [(6, 0, [atts[x] for x in m['attachment_ids'] if x in atts])],
    }).id
    xid_set('mail.message', m['id'], mid)

for line in log:
    print('LOG', line)
print('COUNTS categories=%d projects=%d stages=%d tasks=%d orders=%d lists=%d attachments=%d messages=%d' % (
    len(cats), len(projects), len(stages), len(tasks), len(orders), len(lists), len(atts),
    IMD.search_count([('module', '=', MOD), ('model', '=', 'mail.message')])))
for p in problems:
    print('PROBLEM', p)
if problems:
    env.cr.rollback()
    print('ROLLED_BACK')
else:
    env.cr.commit()
    print('COMMITTED')

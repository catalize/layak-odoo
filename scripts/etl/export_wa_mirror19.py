# Export LAYAK School registration data (layak_wa_registration) from the Odoo 19 mirror (read-only).
# System parameters are NOT exported here (they hold secrets); see README for the direct DB-to-DB copy.
import json
import os

OUT = os.environ.get('ETL_OUT', '/xfer/export_wa.json')
BINARY_FIELDS = {'proof_image'}
SKIP_FIELDS = {'id', 'create_uid', 'write_uid', 'create_date', 'write_date', '__last_update', 'display_name',
               'message_ids', 'message_follower_ids', 'activity_ids', 'website_message_ids', 'message_main_attachment_id',
               'rating_ids', 'message_partner_ids'}


def dump(model, domain=None, only=None):
    M = env[model].with_context(active_test=False)
    names = [n for n, f in M._fields.items()
             if f.store and not f.compute and n not in SKIP_FIELDS and f.type != 'one2many'
             and (f.type != 'binary' or n in BINARY_FIELDS) and (only is None or n in only)]
    out = []
    for r in M.search(domain or []):
        row = {'id': r.id, 'create_date': str(r.create_date)}
        for n in names:
            f = M._fields[n]
            v = r[n]
            if f.type == 'many2one':
                v = v.id or None
            elif f.type == 'many2many':
                v = v.ids
            elif f.type in ('date', 'datetime'):
                v = str(v) if v else None
            elif f.type == 'monetary' or f.type == 'float':
                v = float(v or 0)
            elif f.type == 'binary':
                v = v.decode() if isinstance(v, bytes) else (v or None)
            row[n] = v
        out.append(row)
    rel = {n: M._fields[n].comodel_name for n in names if M._fields[n].type in ('many2one', 'many2many')}
    return {'fields': names, 'relations': rel, 'rows': out}


wa_models = ['layak.wa.bank', 'layak.wa.promo.group', 'layak.wa.promo', 'layak.wa.registration',
             'layak.wa.installment', 'layak.wa.paylink', 'layak.wa.message']
data = {m: dump(m) for m in wa_models if m in env}

users = env['res.users'].with_context(active_test=False).search([('share', '=', False), ('active', '=', True)])
data['res.users'] = [{
    'id': u.id, 'login': u.login, 'name': u.name, 'email': u.email or None, 'partner_id': u.partner_id.id,
    'wa_admin': u.wa_admin, 'wa_super_admin': u.wa_super_admin, 'wa_phone': u.wa_phone or None,
    'group_xmlids': sorted(x for x in u.group_ids.get_external_id().values() if x),
} for u in users]

company_partner = env.company.partner_id.id
data['company_partner_id'] = company_partner
data['res.partner'] = dump('res.partner', [('id', '!=', company_partner), ('user_ids', '=', False)],
                           only={'name', 'is_company', 'type', 'parent_id', 'email', 'phone', 'street', 'street2', 'city',
                                 'zip', 'country_id', 'state_id', 'vat', 'comment', 'wa_phone', 'active', 'lang'})
data['partner_user_map'] = {u.partner_id.id: u.login for u in users}

tmpl = env['product.template'].with_context(active_test=False).search([])
data['product.template'] = dump('product.template', [('id', 'in', tmpl.ids)],
                                only={'name', 'type', 'list_price', 'sale_ok', 'purchase_ok', 'active', 'description_sale',
                                      'default_code'} | {n for n in env['product.template']._fields if n.startswith('wa_')})
data['product_xmlids'] = {t.id: t.get_external_id()[t.id] for t in tmpl if t.get_external_id()[t.id]}
data['product_variant_tmpl'] = {p.id: p.product_tmpl_id.id for p in env['product.product'].with_context(active_test=False).search([])}

atts = env['ir.attachment'].sudo().search(['|', ('res_model', 'like', 'layak.wa.%'),
                                          ('id', 'in', [r['proof_attachment_id'] for r in data['layak.wa.message']['rows'] if r.get('proof_attachment_id')])])
data['ir.attachment'] = [{'id': a.id, 'name': a.name, 'mimetype': a.mimetype, 'res_model': a.res_model, 'res_id': a.res_id,
                          'datas': a.datas.decode() if a.datas else None} for a in atts]
data['countries'] = {c.id: c.code for c in env['res.country'].search([])}

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1, default=str)
print('EXPORT_WA_OK', {m: len(v['rows']) for m, v in data.items() if isinstance(v, dict) and 'rows' in v},
      'users=%d attachments=%d' % (len(data['res.users']), len(data['ir.attachment'])))

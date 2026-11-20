# Export projects, sales, product categories, mailing lists and chatter notes from the Odoo 19 mirror (read-only).
import json
import os

OUT = os.environ.get('ETL_OUT', '/xfer/export_apps.json')


def rows(model, domain, fields):
    M = env[model].with_context(active_test=False)
    out = []
    for r in M.search(domain, order='id'):
        row = {'id': r.id}
        for n in fields:
            f = M._fields[n]
            v = r[n]
            if f.type == 'many2one':
                v = v.id or None
            elif f.type in ('many2many', 'one2many'):
                v = v.ids
            elif f.type in ('date', 'datetime'):
                v = str(v) if v else None
            elif f.type in ('float', 'monetary'):
                v = float(v or 0)
            elif f.type == 'html':
                v = str(v) if v else None
            row[n] = v
        out.append(row)
    return out


data = {}
data['product.category'] = rows('product.category', [], ['name', 'parent_id', 'complete_name'])
data['product_categ'] = {t.id: t.categ_id.id or None for t in env['product.template'].with_context(active_test=False).search([])}

proj_fields = [f for f in ('name', 'description', 'partner_id', 'user_id', 'privacy_visibility', 'date_start', 'date',
                           'type_ids', 'active', 'sequence', 'color', 'label_tasks', 'allow_milestones') if f in env['project.project']._fields]
data['project.project'] = rows('project.project', [], proj_fields)
data['project.task.type'] = rows('project.task.type', [('project_ids', '!=', False)], ['name', 'sequence', 'fold', 'project_ids'])
task_fields = [f for f in ('name', 'description', 'project_id', 'stage_id', 'user_ids', 'priority', 'date_deadline', 'parent_id',
                           'state', 'sequence', 'tag_ids', 'partner_id', 'allocated_hours', 'date_assign', 'date_end', 'color', 'active')
               if f in env['project.task']._fields]
data['project.task'] = rows('project.task', [('project_id', '!=', False)], task_fields)
data['project.tags'] = rows('project.tags', [], ['name', 'color'])

so_fields = ['name', 'state', 'date_order', 'validity_date', 'partner_id', 'partner_invoice_id', 'partner_shipping_id', 'user_id',
             'note', 'client_order_ref', 'origin', 'payment_term_id', 'pricelist_id']
data['sale.order'] = rows('sale.order', [], [f for f in so_fields if f in env['sale.order']._fields])
data['sale.order.line'] = rows('sale.order.line', [], ['order_id', 'sequence', 'display_type', 'name', 'product_id',
                                                     'product_uom_qty', 'price_unit', 'discount', 'tax_ids' if 'tax_ids' in env['sale.order.line']._fields else 'tax_id'])
data['product_variant_tmpl'] = {p.id: p.product_tmpl_id.id for p in env['product.product'].with_context(active_test=False).search([])}
data['taxes'] = {t.id: {'name': t.name, 'amount': t.amount, 'type_tax_use': t.type_tax_use, 'amount_type': t.amount_type}
                 for t in env['account.tax'].with_context(active_test=False).search([])}

data['mailing.list'] = rows('mailing.list', [], ['name', 'is_public', 'active'])
data['mailing.contact'] = rows('mailing.contact', [], ['name', 'email', 'company_name', 'list_ids'])

# Human-written chatter only (notes / messages), not automatic tracking logs
msgs = env['mail.message'].sudo().search([
    ('model', 'in', ['project.task', 'project.project', 'sale.order', 'layak.wa.registration', 'account.move', 'res.partner']),
    ('message_type', '=', 'comment')], order='id')
data['mail.message'] = [{
    'id': m.id, 'model': m.model, 'res_id': m.res_id, 'body': str(m.body or ''), 'date': str(m.date),
    'author_id': m.author_id.id or None, 'is_note': m.subtype_id == env.ref('mail.mt_note'),
    'attachment_ids': m.attachment_ids.ids,
} for m in msgs]

att = env['ir.attachment'].sudo().search(['|', ('res_model', 'in', ['project.task', 'project.project', 'sale.order']),
                                          ('id', 'in', [a for m in data['mail.message'] for a in m['attachment_ids']])])
data['ir.attachment'] = [{'id': a.id, 'name': a.name, 'mimetype': a.mimetype, 'res_model': a.res_model, 'res_id': a.res_id,
                          'datas': a.datas.decode() if a.datas else None, 'type': a.type, 'url': a.url or None} for a in att]

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1, default=str)
print('EXPORT_APPS_OK', {k: len(v) for k, v in data.items() if isinstance(v, list)})

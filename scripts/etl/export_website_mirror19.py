# Export website content (website 1) and company details from the Odoo 19 mirror (read-only).
# View arch is exported as the raw JSON of all translations.
import json
import os
import re

OUT = os.environ.get('ETL_OUT', '/xfer/export_website.json')
cr = env.cr
website = env['website'].browse(1)
data = {}

# Website settings: plain stored fields, languages, favicon
W = env['website']
skip = {'id', 'create_uid', 'write_uid', 'create_date', 'write_date', 'domain', 'theme_id', 'company_id', 'user_id',
        'default_lang_id', 'language_ids', 'menu_id', 'homepage_url', 'favicon'}
wf = {n: website[n] for n, f in W._fields.items()
      if f.store and not f.compute and n not in skip and f.type in ('char', 'text', 'boolean', 'integer', 'float', 'selection', 'html')}
data['website'] = {'fields': {k: (str(v) if v is not None else None) if not isinstance(v, (bool, int, float)) else v for k, v in wf.items()},
                   'languages': website.language_ids.mapped('code'), 'default_lang': website.default_lang_id.code,
                   'favicon': website.favicon.decode() if website.favicon else None, 'theme': website.theme_id.name}

c = env.company
cfields = ['name', 'street', 'street2', 'city', 'zip', 'phone', 'email', 'website', 'vat', 'company_registry',
           'report_header', 'report_footer', 'company_details']
data['company'] = {n: (str(c[n]) if c[n] else None) for n in cfields if n in c._fields}
data['company'].update(country=c.country_id.code, state=c.state_id.code, logo=c.logo.decode() if c.logo else None)

# Website-specific views with raw translated arch
cr.execute("""
    SELECT v.id, v.key, v.name, v.type, v.mode, v.active, v.priority, v.arch_db::text,
           i.key, i.website_id IS NOT NULL, i.id,
           EXISTS (SELECT 1 FROM ir_ui_view g WHERE g.key = v.key AND g.website_id IS NULL),
           v.customize_show, v.track, v.visibility
      FROM ir_ui_view v LEFT JOIN ir_ui_view i ON i.id = v.inherit_id
     WHERE v.website_id = 1 ORDER BY v.id""")
cols = ['id', 'key', 'name', 'type', 'mode', 'active', 'priority', 'arch_db', 'inherit_key', 'inherit_is_specific', 'inherit_id',
        'has_standard', 'customize_show', 'track', 'visibility']
views = [dict(zip(cols, r)) for r in cr.fetchall()]
for v in views:
    v['arch_db'] = json.loads(v['arch_db']) if v['arch_db'] else None
meta = ['website_meta_title', 'website_meta_description', 'website_meta_keywords', 'website_meta_og_img']
data['views'] = views

# Pages
P = env['website.page'].with_context(active_test=False)
pf = [f for f in ('url', 'name', 'is_published', 'website_indexed', 'header_visible', 'footer_visible', 'header_overlay',
                  'header_color', 'header_text_color', 'is_new_page_template', 'date_publish') if f in P._fields]
data['pages'] = []
for p in P.search([('website_id', 'in', (1, False))]):
    row = {f: (str(p[f]) if f == 'date_publish' and p[f] else p[f]) for f in pf}
    row.update(id=p.id, website_id=p.website_id.id or None, view_key=p.view_id.key, view_id=p.view_id.id,
               view_website_id=p.view_id.website_id.id or None)
    for m in meta:
        if m in P._fields:
            row[m] = p[m] or None
    data['pages'].append(row)

# Menus of website 1 (with translated names)
cr.execute("""SELECT id, name::text, url, parent_id, sequence, new_window, page_id, mega_menu_content IS NOT NULL, mega_menu_content::text, mega_menu_classes
                FROM website_menu WHERE website_id = 1 ORDER BY parent_id NULLS FIRST, sequence, id""")
data['menus'] = [{'id': r[0], 'name': json.loads(r[1]), 'url': r[2], 'parent_id': r[3], 'sequence': r[4], 'new_window': r[5], 'page_id': r[6],
                  'is_mega_menu': r[7], 'mega_menu_content': json.loads(r[8]) if r[8] else None, 'mega_menu_classes': r[9]}
                 for r in cr.fetchall()]
data['top_menu_id'] = website.menu_id.id

# Attachments: images referenced from website views, custom SCSS
ids = set()
for v in views:
    for txt in (v['arch_db'] or {}).values():
        ids.update(int(x) for x in re.findall(r'/web/image/(\d+)', txt or ''))
        ids.update(int(x) for x in re.findall(r'/web/content/(\d+)', txt or ''))
for m in data['menus']:
    for txt in (m['mega_menu_content'] or {}).values():
        ids.update(int(x) for x in re.findall(r'/web/image/(\d+)', txt or ''))
Att = env['ir.attachment'].sudo()
atts = Att.browse(sorted(ids)).exists() | Att.search([('url', '=like', '/_custom/%')])
data['attachments'] = [{
    'id': a.id, 'name': a.name, 'mimetype': a.mimetype, 'url': a.url or None, 'type': a.type, 'public': a.public,
    'res_model': a.res_model, 'res_id': a.res_id, 'website_id': a.website_id.id if 'website_id' in a._fields and a.website_id else None,
    'checksum': a.checksum, 'datas': a.datas.decode() if a.datas else None, 'key': a.key if 'key' in a._fields else None,
} for a in atts]
data['missing_attachment_ids'] = sorted(ids - set(atts.ids))

assets = env['ir.asset'].sudo().with_context(active_test=False).search([('path', '=like', '/_custom/%')])
data['assets'] = [{'name': a.name, 'bundle': a.bundle, 'directive': a.directive, 'path': a.path, 'target': a.target,
                   'active': a.active, 'sequence': a.sequence, 'website': bool(a.website_id)} for a in assets]

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1, default=str)
print('EXPORT_WEBSITE_OK views=%d pages=%d menus=%d attachments=%d (missing %d) assets=%d langs=%s theme=%s' % (
    len(views), len(data['pages']), len(data['menus']), len(data['attachments']), len(data['missing_attachment_ids']),
    len(data['assets']), data['website']['languages'], data['website']['theme']))

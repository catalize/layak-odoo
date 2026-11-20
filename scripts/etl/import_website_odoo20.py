# Import website 1 content and company details (export_website_mirror19.py) into Odoo 20.
# Theme views (theme_cobalt.*) are NOT copied: applying the theme creates Odoo 20's own versions.
# Each view is applied in its own savepoint; views Odoo 20 rejects are reported, the rest is kept.
import base64
import json
import os
import re

d = json.load(open(os.environ.get('ETL_IN', '/xfer/export_website.json'), encoding='utf-8'))
MOD = '__etl_mirror19__'
IMD = env['ir.model.data']
View = env['ir.ui.view'].with_context(active_test=False)
log, failed = [], []


def xid_get(name):
    r = IMD.search([('module', '=', MOD), ('name', '=', name)], limit=1)
    return r.res_id if r else None


def xid_set(name, record):
    if not xid_get(name):
        IMD.create({'module': MOD, 'name': name, 'model': record._name, 'res_id': record.id, 'noupdate': True})


# 1. Languages
lang = env['res.lang'].with_context(active_test=False).search([('code', '=', d['website']['default_lang'])], limit=1)
if not lang.active:
    env['base.language.install'].create({'lang_ids': [(6, 0, lang.ids)], 'overwrite': False}).lang_install()
    log.append('language activated and translations loaded: %s' % lang.code)
site_langs = env['res.lang'].search([('code', 'in', d['website']['languages'])])

# 2. Company details
c = env.company
cd = dict(d['company'])
vals = {k: v for k, v in cd.items() if k in c._fields and k not in ('country', 'state', 'logo') and v}
country = env['res.country'].search([('code', '=', cd.get('country'))], limit=1)
if country:
    vals['country_id'] = country.id
    st = env['res.country.state'].search([('country_id', '=', country.id), ('code', '=', cd.get('state'))], limit=1)
    if st:
        vals['state_id'] = st.id
if cd.get('logo'):
    vals['logo'] = cd['logo']
c.write(vals)

# 3. Website settings
website = env['website'].browse(1)
wvals = {k: v for k, v in d['website']['fields'].items() if k in website._fields and website._fields[k].store
         and not website._fields[k].compute and website._fields[k].type in ('char', 'text', 'boolean', 'integer', 'float', 'selection', 'html')}
for k, f in list(wvals.items()):
    if website._fields[k].type == 'selection' and wvals[k] not in dict(website._fields[k]._description_selection(env)):
        log.append('website field %s value %r not valid in 20, kept default' % (k, wvals.pop(k)))
wvals.update(language_ids=[(6, 0, site_langs.ids)], default_lang_id=lang.id)
if d['website']['favicon']:
    wvals['favicon'] = d['website']['favicon']
website.write(wvals)

# 4. Theme
theme = env['ir.module.module'].search([('name', '=', d['website']['theme'])], limit=1)
if website.theme_id != theme:
    theme.with_context(website_id=website.id).button_choose_theme()
    log.append('theme applied: %s' % theme.name)
env.flush_all()
env.invalidate_all()
website = env['website'].browse(1)

# 5. Attachments (images, custom SCSS) and asset overrides
Att = env['ir.attachment'].sudo()
att_map = {}
for a in d['attachments']:
    tid = xid_get('ir_attachment_%s' % a['id'])
    if not tid:
        v = {'name': a['name'], 'mimetype': a['mimetype'], 'public': a['public'], 'res_model': a['res_model'] or False,
             'res_id': a['res_id'] or 0, 'url': a['url'] or False}
        if 'website_id' in Att._fields and a['website_id']:
            v['website_id'] = website.id
        if 'key' in Att._fields and a.get('key'):
            v['key'] = a['key']
        if a['type'] == 'url':
            v['type'] = 'url'
        else:
            v['raw'] = base64.b64decode(a['datas']) if a['datas'] else b''
        rec = Att.with_context(image_no_postprocess=True).create(v)
        xid_set('ir_attachment_%s' % a['id'], rec)
        tid = rec.id
    att_map[a['id']] = tid
for a in d['assets']:
    Asset = env['ir.asset'].sudo().with_context(active_test=False)
    dom = [('path', '=', a['path']), ('bundle', '=', a['bundle'])]
    if 'website_id' in Asset._fields:
        dom.append(('website_id', '=', website.id if a['website'] else False))
    if not Asset.search(dom, limit=1):
        v = {k: a[k] for k in ('name', 'bundle', 'directive', 'path', 'target', 'active', 'sequence') if k in Asset._fields}
        if a['website'] and 'website_id' in Asset._fields:
            v['website_id'] = website.id
        Asset.create(v)


# Known Odoo 19 -> 20 template changes: (old, new) text replacements applied to the source arch, per view key
PATCHES = {
    'website.contactus': [
        ("request.env['website.visitor']._get_visitor_from_request()", "request.env['ir.http']._get_visitor_from_request()"),
    ],
    'website.footer_copyright_company_name': [
        ("""<xpath expr="//div[hasclass('o_footer_copyright')]//div[hasclass('col-sm')]" position="attributes">""",
         """<xpath expr="//footer//div[@name='footer_copyright_col']" position="attributes">"""),
        ('remove="col-sm text-sm-start" add="col-md d-flex flex-column-reverse gap-2 text-md-start"',
         'remove="text-sm-start" add="d-flex flex-column-reverse gap-2 text-md-start"'),
    ],
}
PATCH_KEY = None


def rewrite(txt):
    if not txt:
        return txt
    for old, new in PATCHES.get(PATCH_KEY, []):
        txt = txt.replace(old, new)
    txt = re.sub(r'/web/image/(\d+)', lambda m: '/web/image/%s' % att_map.get(int(m.group(1)), m.group(1)), txt)
    return re.sub(r'/web/content/(\d+)', lambda m: '/web/content/%s' % att_map.get(int(m.group(1)), m.group(1)), txt)


def set_arch(view, arch_json, active):
    arch = {k: rewrite(v) for k, v in arch_json.items()}
    env.flush_all()
    env.cr.execute('UPDATE ir_ui_view SET arch_db = %s, active = %s WHERE id = %s', [json.dumps(arch), active, view.id])
    env.invalidate_all()
    # validate the stored arch against Odoo 20 (raises if a parent xpath no longer matches)
    View.browse(view.id)._check_xml()


# 6. Views, parents before children
src_by_id = {v['id']: v for v in d['views']}
todo = [v for v in d['views'] if not (v['key'] or '').startswith('theme_')]
view_map = {}
progress = True
while todo and progress:
    progress, rest = False, []
    for v in todo:
        if v['inherit_is_specific'] and v['inherit_id'] not in view_map:
            parent_src = src_by_id.get(v['inherit_id'])
            if parent_src and not (parent_src['key'] or '').startswith('theme_'):
                rest.append(v)
                continue
        progress = True
        PATCH_KEY = v['key']
        try:
            with env.cr.savepoint():
                en = rewrite((v['arch_db'] or {}).get('en_US') or '')
                target = View.search([('key', '=', v['key']), ('website_id', '=', website.id)], limit=1)
                if not target and v['has_standard']:
                    generic = View.search([('key', '=', v['key']), ('website_id', '=', False)], limit=1)
                    if not generic:
                        raise ValueError('standard view %s does not exist in Odoo 20' % v['key'])
                    generic.with_context(website_id=website.id).write({'arch': en, 'active': v['active']})
                    target = View.search([('key', '=', v['key']), ('website_id', '=', website.id)], limit=1)
                if not target:
                    if v['inherit_is_specific']:
                        parent = View.browse(view_map.get(v['inherit_id'])) if v['inherit_id'] in view_map else \
                            View.search([('key', '=', src_by_id[v['inherit_id']]['key']), ('website_id', '=', website.id)], limit=1)
                    elif v['inherit_key']:
                        parent = View.search([('key', '=', v['inherit_key']), ('website_id', '=', False)], limit=1)
                    else:
                        parent = View.browse()
                    if v['inherit_key'] and not parent:
                        raise ValueError('parent %s does not exist in Odoo 20' % v['inherit_key'])
                    target = View.create({'key': v['key'], 'name': v['name'], 'type': v['type'], 'mode': v['mode'],
                                          'priority': v['priority'], 'website_id': website.id, 'inherit_id': parent.id or False,
                                          'arch': en, 'customize_show': v['customize_show'], 'track': v['track']})
                set_arch(target, v['arch_db'] or {'en_US': en}, v['active'])
                view_map[v['id']] = target.id
        except Exception as e:
            failed.append((v['key'], v['active'], str(e).splitlines()[0][:220]))
    todo = rest
for v in todo:
    failed.append((v['key'], v['active'], 'parent view could not be migrated'))

# 7. Pages (website-specific ones; standard pages come from the modules)
Page = env['website.page'].with_context(active_test=False)
pfields = ['name', 'is_published', 'website_indexed', 'header_visible', 'footer_visible', 'header_overlay', 'header_color',
           'header_text_color', 'website_meta_title', 'website_meta_description', 'website_meta_keywords', 'website_meta_og_img']
for p in d['pages']:
    if not p['website_id']:
        continue
    view_id = view_map.get(p['view_id']) or View.search([('key', '=', p['view_key']), ('website_id', '=', website.id)], limit=1).id
    if not view_id:
        failed.append(('page ' + p['url'], True, 'view %s missing' % p['view_key']))
        continue
    vals = {k: p[k] for k in pfields if k in p and k in Page._fields}
    vals['website_meta_og_img'] = rewrite(vals.get('website_meta_og_img') or '') or False
    page = Page.search([('url', '=', p['url']), ('website_id', '=', website.id)], limit=1)
    if page:
        page.write(dict(vals, view_id=view_id))
    else:
        Page.create(dict(vals, url=p['url'], view_id=view_id, website_id=website.id))

# 8. Menus: replace website 1 menu tree with the source tree
Menu = env['website.menu']
top = website.menu_id
src_top = next(m for m in d['menus'] if m['id'] == d['top_menu_id'])
Menu.search([('website_id', '=', website.id), ('id', 'child_of', top.id), ('id', '!=', top.id)]).unlink()
menu_map = {src_top['id']: top.id}
for m in d['menus']:
    if m['id'] == src_top['id']:
        continue
    rec = Menu.create({'name': m['name'].get('en_US') or next(iter(m['name'].values())), 'url': m['url'], 'sequence': m['sequence'],
                       'new_window': m['new_window'], 'website_id': website.id, 'parent_id': menu_map.get(m['parent_id'], top.id),
                       'is_mega_menu': bool(m.get('is_mega_menu')), 'mega_menu_classes': m.get('mega_menu_classes') or False})
    env.flush_all()
    env.cr.execute('UPDATE website_menu SET name = %s WHERE id = %s', [json.dumps(m['name']), rec.id])
    if m.get('mega_menu_content'):
        env.cr.execute('UPDATE website_menu SET mega_menu_content = %s WHERE id = %s',
                       [json.dumps({k: rewrite(v) for k, v in m['mega_menu_content'].items()}), rec.id])
    menu_map[m['id']] = rec.id
env.invalidate_all()

for line in log:
    print('LOG', line)
print('RESULT views migrated=%d failed=%d (theme views skipped=%d) attachments=%d' % (
    len(view_map), len(failed), sum(1 for v in d['views'] if (v['key'] or '').startswith('theme_')), len(att_map)))
for k, active, err in failed:
    print('FAILED %s [%s] %s' % (k, 'active' if active else 'inactive', err))
env.cr.commit()
print('COMMITTED')

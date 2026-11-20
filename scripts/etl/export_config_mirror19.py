# Export remaining configuration from the Odoo 19 mirror (read-only): company bank accounts and the journals
# using them, document layout, user time zones/languages.
import json
import os

OUT = os.environ.get('ETL_OUT', '/xfer/export_config.json')
c = env.company
data = {
    'banks': [{
        'acc_number': b.acc_number, 'bank_name': b.bank_id.name or None, 'bic': b.bank_id.bic or None,
        'holder': b.acc_holder_name or None, 'allow_out_payment': b.allow_out_payment,
        'journals': env['account.journal'].search([('bank_account_id', '=', b.id)]).mapped('code'),
    } for b in env['res.partner.bank'].search([('partner_id', '=', c.partner_id.id)])],
    'layout': {
        'external_report_layout': c.external_report_layout_id.key or None,
        'font': c.font if 'font' in c._fields else None,
        'primary_color': c.primary_color or None, 'secondary_color': c.secondary_color or None,
        'layout_background': c.layout_background if 'layout_background' in c._fields else None,
        'paperformat': c.paperformat_id.name or None,
    },
    'users': [{'login': u.login, 'tz': u.tz or None, 'lang': u.lang or None}
              for u in env['res.users'].search([('share', '=', False)])],
}
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print('EXPORT_CONFIG_OK banks=%d users=%d layout=%s' % (len(data['banks']), len(data['users']), data['layout']['external_report_layout']))

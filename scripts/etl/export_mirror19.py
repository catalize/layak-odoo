# Export accounting data from the Odoo 19 mirror to JSON (read-only).
# Run inside the mirror web container via `odoo shell`, see scripts/etl/README.md.
# Output: $ETL_OUT (default /xfer/export.json)
import json
import os
from collections import defaultdict

OUT = os.environ.get('ETL_OUT', '/xfer/export.json')


def tr(v):
    return v if not isinstance(v, dict) else (v.get('en_US') or next(iter(v.values()), ''))


Account = env['account.account'].with_context(active_test=False)
has_active = 'active' in Account._fields

accounts = [{
    'id': a.id, 'code': a.code, 'name': a.name, 'account_type': a.account_type,
    'reconcile': a.reconcile, 'active': a.active if has_active else True,
} for a in Account.search([])]

journals = [{
    'id': j.id, 'code': j.code, 'name': j.name, 'type': j.type, 'active': j.active,
    'default_account_code': j.default_account_id.code or None,
} for j in env['account.journal'].with_context(active_test=False).search([])]

plans = [{'id': p.id, 'name': p.name} for p in env['account.analytic.plan'].search([])]
analytics = [{
    'id': a.id, 'name': a.name, 'code': a.code or None, 'plan_id': a.root_plan_id.id, 'active': a.active,
} for a in env['account.analytic.account'].with_context(active_test=False).search([])]

moves = env['account.move'].search([('state', 'in', ('posted', 'draft'))], order='date, name, id')
partner_ids = set()
out_moves = []
for m in moves:
    lines = []
    for l in m.line_ids.sorted('id'):
        if l.display_type not in (False, 'product', 'payment_term'):
            continue
        if l.partner_id:
            partner_ids.add(l.partner_id.id)
        lines.append({
            'account_code': l.account_id.code, 'name': l.name or '', 'debit': l.debit, 'credit': l.credit,
            'partner_id': l.partner_id.id or None, 'analytic_distribution': l.analytic_distribution or None,
            'date_maturity': str(l.date_maturity) if l.date_maturity else None,
        })
    out_moves.append({
        'id': m.id, 'name': m.name, 'date': str(m.date), 'ref': m.ref or None, 'state': m.state,
        'journal_code': m.journal_id.code, 'narration': m.narration or None, 'move_type': m.move_type,
        'partner_id': m.partner_id.id or None, 'lines': lines,
    })
    if m.partner_id:
        partner_ids.add(m.partner_id.id)

partners = [{
    'id': p.id, 'name': p.name, 'is_company': p.is_company, 'email': p.email or None, 'phone': p.phone or None,
} for p in env['res.partner'].with_context(active_test=False).browse(sorted(partner_ids))]

# Control totals: posted balance per account code per month, and per journal bank account.
totals = defaultdict(float)
env.cr.execute("""
    select l.account_id, to_char(l.date, 'YYYY-MM'), sum(l.balance)
      from account_move_line l where l.parent_state = 'posted' group by 1, 2
""")
code_of = {a['id']: a['code'] for a in accounts}
for acc_id, month, bal in env.cr.fetchall():
    totals['%s|%s' % (code_of[acc_id], month)] += float(bal)

data = {
    'source': {'db': env.cr.dbname, 'company': env.company.name, 'currency': env.company.currency_id.name},
    'accounts': accounts, 'journals': journals, 'analytic_plans': plans, 'analytic_accounts': analytics,
    'partners': partners, 'moves': out_moves,
    'control_totals': {k: round(v, 2) for k, v in sorted(totals.items())},
}
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print('EXPORT_OK accounts=%d journals=%d plans=%d analytics=%d partners=%d moves=%d (posted=%d draft=%d) lines=%d -> %s' % (
    len(accounts), len(journals), len(plans), len(analytics), len(partners), len(out_moves),
    sum(m['state'] == 'posted' for m in out_moves), sum(m['state'] == 'draft' for m in out_moves),
    sum(len(m['lines']) for m in out_moves), OUT))

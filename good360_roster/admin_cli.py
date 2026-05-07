#!/usr/bin/env python3
"""Good360 Roster Admin CLI"""
import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

_ROSTER_DIR = Path(__file__).resolve().parent
_WORKDIR = os.environ.get('WORKDIR', '/a0/usr/workdir')
DB_PATH = Path(f'{_WORKDIR}/good360_roster/db/roster.db')
sys.path.insert(0, str(_ROSTER_DIR))
from vault import encrypt_field, load_env

load_env()

def get_db():
    c = sqlite3.connect(str(DB_PATH), timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    return c

def inp_req(p):
    while True:
        v = input(p).strip()
        if v: return v
        print('  [Required]')

def inp_opt(p):
    return input(p).strip() or None

def inp_bool(p, default=False):
    ds = 'Y' if default else 'N'
    while True:
        v = input('%s [%s]: ' % (p, ds)).strip().lower()
        if not v: return default
        if v in ('y','yes'): return True
        if v in ('n','no'): return False
        print('  Y or N')

def fmt_phone(raw):
    d = ''.join(c for c in raw if c.isdigit())
    if len(d) == 10: return '+1' + d
    if len(d) == 11 and d[0] == '1': return '+' + d
    return raw if raw.startswith('+') else '+' + d

def card_type(num):
    n = num.replace(' ', '')
    if n.startswith('4'): return 'visa'
    if n.startswith('5') or (n.startswith('2') and len(n) >= 2): return 'mastercard'
    if n.startswith('34') or n.startswith('37'): return 'amex'
    if n.startswith('6'): return 'discover'
    return 'unknown'

SQL_INSERT_ORG = """
INSERT INTO nonprofits
(uuid, org_name, contact_name, contact_email, contact_phone,
 alert_email, phone_number, sms_alerts_enabled, auto_buy_global,
 master_card_fallback, max_price_override, notes, joined_date,
 status, queue_position, subscription_active, created_at, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

def add_org():
    print('\n=== Add New Org ===\n')
    on = inp_req('  Org Name: ')
    cn = inp_req('  Contact Name: ')
    em = inp_req('  Contact Email: ')
    ph_raw = inp_req('  Phone: ')
    ph = fmt_phone(ph_raw)
    ae = inp_opt('  Alert Email (Enter = contact): ')
    if not ae: ae = em
    sms = inp_bool('  SMS Alerts?', default=False)
    auto = inp_bool('  Auto-Buy?', default=False)
    mfb = inp_bool('  Master Card Fallback?', default=False)
    mp = inp_opt('  Max Price (USD): ')
    notes = inp_opt('  Notes: ')
    ou = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        cur = conn.execute(SQL_INSERT_ORG,
            (ou, on, cn, em, ph_raw, ae, ph, int(sms), int(auto), int(mfb),
             float(mp) if mp else None, notes, now, 'pending_setup', 0, 0, now, now))
        conn.commit()
        print('  OK id=' + str(cur.lastrowid))
        return cur.lastrowid
    finally:
        conn.close()

def add_credentials(org_id):
    print('\n=== Credentials #' + str(org_id) + ' ===\n')
    em = inp_req('  Good360 Email: ')
    pw = inp_req('  Good360 Password: ')
    g3 = inp_opt('  Good360 Org ID: ')
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO nonprofit_credentials (nonprofit_id,email,password_enc,good360_org_id,login_verified) VALUES (?,?,?,?,0)',
            (org_id, em, encrypt_field(pw), g3))
        conn.commit()
        print('  Saved (encrypted)')
    finally:
        conn.close()

def add_payment(org_id):
    print('\n=== Payment #' + str(org_id) + ' ===\n')
    num = inp_req('  Card Number: ').replace(' ', '')
    holder = inp_req('  Cardholder Name: ')
    mm = inp_req('  Expiry MM: ')
    yy = inp_req('  Expiry YY: ')
    cvv = inp_req('  CVV: ')
    bzip = inp_req('  Billing ZIP: ')
    pri = inp_req('  Priority (1=primary,2=backup1,3=backup2): ')
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO nonprofit_payment_methods (nonprofit_id,priority,card_holder_name,card_number_enc,card_last4,card_expiry_month,card_expiry_year,card_cvv_enc,billing_zip,card_type,is_active) VALUES (?,?,?,?,?,?,?,?,?,?,1)',
            (org_id, pri, holder, encrypt_field(num), num[-4:], mm, yy, encrypt_field(cvv), bzip, card_type(num)))
        conn.commit()
        print('  Saved (encrypted)')
    finally:
        conn.close()

def add_address(org_id):
    print('\n=== Address #' + str(org_id) + ' ===\n')
    label = inp_opt('  Label: ') or 'Primary'
    l1 = inp_req('  Street Line1: ')
    l2 = inp_opt('  Street Line2: ')
    city = inp_req('  City: ')
    state = inp_req('  State: ')
    zc = inp_req('  ZIP: ')
    country = inp_opt('  Country (default US): ') or 'US'
    prim = inp_bool('  Primary?', default=True)
    conn = get_db()
    try:
        if prim:
            conn.execute('UPDATE nonprofit_addresses SET is_primary=0 WHERE nonprofit_id=?', (org_id,))
        conn.execute(
            'INSERT INTO nonprofit_addresses (nonprofit_id,address_label,street_line1,street_line2,city,state,zip_code,country,is_primary) VALUES (?,?,?,?,?,?,?,?,?)',
            (org_id, label, l1, l2, city, state, zc, country, int(prim)))
        conn.commit()
        print('  Saved')
    finally:
        conn.close()

def setup_cats(org_id):
    print('\n=== Categories #' + str(org_id) + ' ===\n')
    cats = [
        ('amazon_new_unsorted', 'Amazon New Unsorted'),
        ('amazon_variety',      'Amazon Variety'),
        ('amazon_houseware',    'Amazon Houseware'),
        ('softlines',           'Softlines'),
        ('other',              'Other'),
    ]
    for key, label in cats:
        print('\n  [' + key + '] ' + label)
        auto = inp_bool('  Auto-Buy?', default=False)
        excl = inp_bool('  Exclude?', default=False)
        mp = inp_opt('  Max price: ')
        conn = get_db()
        try:
            conn.execute(
                'INSERT OR REPLACE INTO nonprofit_category_preferences (nonprofit_id,category_key,category_label,auto_buy_enabled,max_price_override,is_excluded) VALUES (?,?,?,?,?,?)',
                (org_id, key, label, int(auto), float(mp) if mp else None, int(excl)))
            conn.commit()
        finally:
            conn.close()
    print('\n  Done')

def list_orgs():
    print('\n=== Organizations ===')
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT id,org_name,contact_email,status,auto_buy_global,queue_position FROM nonprofits ORDER BY id').fetchall()
        if not rows:
            print('  (none)')
            return
        print('  #    Name                          Email                            Status       AB   Queue')
        print('  ' + '-'*85)
        for r in rows:
            ab = 'ON' if r['auto_buy_global'] else 'OFF'
            print('  %-4d %-28s %-28s %-12s %-3s %s' % (r['id'], r['org_name'][:28], r['contact_email'][:28], r['status'], ab, r['queue_position']))
    finally:
        conn.close()

def show_org(org_id):
    conn = get_db()
    try:
        org = conn.execute('SELECT * FROM nonprofits WHERE id=?', (org_id,)).fetchone()
        if not org:
            print('Org #' + str(org_id) + ' not found.')
            return
        print('\n=== Org #' + str(org_id) + ': ' + org['org_name'] + ' ===')
        for k, v in dict(org).items():
            if k.endswith('_enc') or k == 'password_enc':
                v = '[ENCRYPTED]'
            print('  %s: %s' % (k, v))
        creds = conn.execute(
            'SELECT email,good360_org_id,login_verified FROM nonprofit_credentials WHERE nonprofit_id=?', (org_id,)).fetchall()
        if creds:
            print('  Credentials:')
            for c in creds:
                print('    %s  g360=%s  verified=%s' % (c['email'], c['good360_org_id'], c['login_verified']))
        cards = conn.execute(
            'SELECT priority,card_last4,card_type,is_active FROM nonprofit_payment_methods WHERE nonprofit_id=?', (org_id,)).fetchall()
        if cards:
            print('  Payments:')
            for c in cards:
                print('    Pri=%s ****%s %s active=%s' % (c['priority'], c['card_last4'], c['card_type'], c['is_active']))
        cats = conn.execute(
            'SELECT category_key,auto_buy_enabled,is_excluded FROM nonprofit_category_preferences WHERE nonprofit_id=?', (org_id,)).fetchall()
        if cats:
            print('  Categories:')
            for c in cats:
                flags = []
                if c['auto_buy_enabled']: flags.append('AUTO')
                if c['is_excluded']: flags.append('EXCL')
                flagstr = ' '.join(flags) or 'passive'
                print('    %-25s %s' % (c['category_key'], flagstr))
    finally:
        conn.close()

def activate_org(org_id):
    from queue_manager import assign_queue_positions
    conn = get_db()
    try:
        conn.execute('UPDATE nonprofits SET status=? WHERE id=?', ('active', org_id))
        conn.commit()
        assign_queue_positions()
        print('Org #' + str(org_id) + ' activated.')
    finally:
        conn.close()

def suspend_org(org_id):
    conn = get_db()
    try:
        conn.execute('UPDATE nonprofits SET status=? WHERE id=?', ('suspended', org_id))
        conn.commit()
        print('Org #' + str(org_id) + ' suspended.')
    finally:
        conn.close()

def main():
    p = argparse.ArgumentParser(prog='admin_cli.py', description='Good360 Roster Admin CLI')
    sub = p.add_subparsers(dest='cmd')
    add = sub.add_parser('add-org')
    add.add_argument('--skip-creds',    action='store_true')
    add.add_argument('--skip-payments', action='store_true')
    add.add_argument('--skip-address',  action='store_true')
    add.add_argument('--skip-cats',     action='store_true')
    sub.add_parser('list-orgs')
    sp = sub.add_parser('show-org'); sp.add_argument('org_id', type=int)
    sc = sub.add_parser('add-credentials'); sc.add_argument('org_id', type=int)
    sp2 = sub.add_parser('add-payment'); sp2.add_argument('org_id', type=int)
    sa = sub.add_parser('add-address'); sa.add_argument('org_id', type=int)
    sc2 = sub.add_parser('setup-categories'); sc2.add_argument('org_id', type=int)
    sac = sub.add_parser('activate'); sac.add_argument('org_id', type=int)
    ssc = sub.add_parser('suspend'); ssc.add_argument('org_id', type=int)
    args = p.parse_args()
    if args.cmd == 'add-org':
        oid = add_org()
        if not args.skip_creds:    add_credentials(oid)
        if not args.skip_payments: add_payment(oid)
        if not args.skip_address:  add_address(oid)
        if not args.skip_cats:     setup_cats(oid)
        print('\nDone! Activate: admin_cli.py activate ' + str(oid))
    elif args.cmd == 'list-orgs':        list_orgs()
    elif args.cmd == 'show-org':         show_org(args.org_id)
    elif args.cmd == 'add-credentials':  add_credentials(args.org_id)
    elif args.cmd == 'add-payment':      add_payment(args.org_id)
    elif args.cmd == 'add-address':      add_address(args.org_id)
    elif args.cmd == 'setup-categories': setup_cats(args.org_id)
    elif args.cmd == 'activate':         activate_org(args.org_id)
    elif args.cmd == 'suspend':          suspend_org(args.org_id)
    else:
        p.print_help()

if __name__ == '__main__':
    main()

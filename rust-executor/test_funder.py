import os, sys, re
sys.path.insert(0, '/opt/pairbot/pair_engine_package')
from dotenv import load_dotenv
load_dotenv('/opt/pairbot/pair_engine_package/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, CreateOrderOptions, OrderType

creds = ApiCreds(
    api_key=os.environ['POLY_API_KEY'],
    api_secret=os.environ['POLY_API_SECRET'],
    api_passphrase=os.environ['POLY_API_PASSPHRASE'],
)

client1 = ClobClient('https://clob.polymarket.com', chain_id=137,
    key=os.environ['POLY_PRIVATE_KEY'], creds=creds)

client2 = ClobClient('https://clob.polymarket.com', chain_id=137,
    key=os.environ['POLY_PRIVATE_KEY'], creds=creds,
    funder=os.environ.get('POLY_WALLET_ADDRESS'))

log = open('/opt/pairbot/pair_engine_package/rust_orders.log').read()
m = re.search(r'tokenId":"(\d+)"', log)
tid = m.group(1) if m else None
print("Token:", tid)

if tid:
    for label, c in [('NO_FUNDER', client1), ('WITH_FUNDER', client2)]:
        args = OrderArgs(token_id=tid, price=0.01, size=5.0, side='BUY', fee_rate_bps=1000)
        opts = CreateOrderOptions(tick_size='0.01', neg_risk=False)
        signed = c.builder.create_order(args, opts)
        d = signed.order.dict()
        print(f"\n{label}: maker={d['maker'][:20]}... sigType={d['signatureType']}")
        try:
            r = c.post_order(signed, OrderType.GTC, post_only=True)
            print(f"  SUCCESS: {r}")
            if isinstance(r, dict) and r.get('orderID'):
                c.cancel(r['orderID'])
        except Exception as e:
            print(f"  ERROR: {str(e)[:200]}")

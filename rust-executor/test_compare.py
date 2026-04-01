import os, sys, json
sys.path.insert(0, '/opt/pairbot/pair_engine_package')
from dotenv import load_dotenv
load_dotenv('/opt/pairbot/pair_engine_package/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, CreateOrderOptions, OrderType
from py_clob_client.utilities import order_to_json

creds = ApiCreds(
    api_key=os.environ['POLY_API_KEY'],
    api_secret=os.environ['POLY_API_SECRET'],
    api_passphrase=os.environ['POLY_API_PASSPHRASE'],
)
client = ClobClient('https://clob.polymarket.com', chain_id=137,
    key=os.environ['POLY_PRIVATE_KEY'], creds=creds,
    funder=os.environ['POLY_WALLET_ADDRESS'])

# Same token as Rust just tried
tid = '83666677302746465594656442937382390381817039552926856310133426704936276234911'
args = OrderArgs(token_id=tid, price=0.75, size=10.67, side='BUY', fee_rate_bps=1000)
opts = CreateOrderOptions(tick_size='0.01', neg_risk=False)
signed = client.builder.create_order(args, opts)

d = signed.order.dict()
print("Python maker:", d['maker'])
print("Python signer:", d['signer'])
print("Python makerAmount:", d['makerAmount'])
print("Python takerAmount:", d['takerAmount'])
print("Python signatureType:", d['signatureType'])

# Post it
try:
    r = client.post_order(signed, OrderType.GTC, post_only=True)
    print("PYTHON SUCCESS:", r)
    if isinstance(r, dict) and r.get('orderID'):
        client.cancel(r['orderID'])
except Exception as e:
    print("PYTHON ERROR:", str(e)[:300])

import os, sys, re, time, json, asyncio, aiohttp
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

client_nf = ClobClient('https://clob.polymarket.com', chain_id=137,
    key=os.environ['POLY_PRIVATE_KEY'], creds=creds)

client_f = ClobClient('https://clob.polymarket.com', chain_id=137,
    key=os.environ['POLY_PRIVATE_KEY'], creds=creds,
    funder=os.environ.get('POLY_WALLET_ADDRESS'))

# Get current active token from bot via WS
async def get_tid():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect('http://localhost:8080/ws') as ws:
            for i in range(30):
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                data = json.loads(msg.data)
                if 'active_markets' in data:
                    for slug, m in data['active_markets'].items():
                        up = m.get('up_price', 0)
                        return slug, up
    return None, 0

slug, up = asyncio.run(get_tid())
print(f"Market: {slug} UP={up}")

# Get tokens from gamma
import requests
resp = requests.get(f'https://gamma-api.polymarket.com/events/slug/{slug}', timeout=10)
event = resp.json()
markets = event.get('markets', [])
tid = None
for m in markets:
    tids = m.get('clobTokenIds', '')
    if isinstance(tids, str):
        tids = json.loads(tids) if tids.startswith('[') else [tids]
    if isinstance(tids, list) and tids:
        tid = tids[0]  # UP token
        break

print(f"Token: {tid}")

if tid:
    for label, c in [('NO_FUNDER', client_nf), ('WITH_FUNDER', client_f)]:
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

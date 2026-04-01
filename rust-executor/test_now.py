import os, sys, json, requests, time
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
client = ClobClient('https://clob.polymarket.com', chain_id=137,
    key=os.environ['POLY_PRIVATE_KEY'], creds=creds,
    funder=os.environ['POLY_WALLET_ADDRESS'])

now = int(time.time())
window = (now // 300) * 300
slug = "btc-updown-5m-" + str(window)
print("NOW:", time.strftime('%H:%M:%S'), "Slug:", slug)

resp = requests.get("https://gamma-api.polymarket.com/events/slug/" + slug, timeout=10)
event = resp.json()
for m in event.get('markets', []):
    tids = m.get('clobTokenIds', '')
    if isinstance(tids, str):
        tids = json.loads(tids)
    if tids:
        tid = tids[0]
        print("Token:", tid)
        args = OrderArgs(token_id=tid, price=0.01, size=5.0, side='BUY', fee_rate_bps=1000)
        opts = CreateOrderOptions(tick_size='0.01', neg_risk=False)
        signed = client.builder.create_order(args, opts)
        try:
            r = client.post_order(signed, OrderType.GTC, post_only=True)
            print("SUCCESS:", r)
            if isinstance(r, dict) and r.get('orderID'):
                client.cancel(r['orderID'])
        except Exception as e:
            print("ERROR:", str(e)[:200])

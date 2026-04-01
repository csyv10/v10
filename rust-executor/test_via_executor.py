import os, sys, json, time, requests
sys.path.insert(0, '/opt/pairbot/pair_engine_package')
from dotenv import load_dotenv
load_dotenv('/opt/pairbot/pair_engine_package/.env')

# Import LiveExecutor which has the patched HTTP
from live_executor import LiveExecutor

# Create it - it will initialize the py_clob_client
ex = LiveExecutor()
print("Mode:", "LIVE" if ex.live else "PAPER")

# Get active token
now = int(time.time())
window = (now // 300) * 300
slug = 'btc-updown-5m-' + str(window)
resp = requests.get('https://gamma-api.polymarket.com/events/slug/' + slug, timeout=10)
event = resp.json()
tid = None
for m in event.get('markets', []):
    tids = m.get('clobTokenIds', '')
    if isinstance(tids, str):
        tids = json.loads(tids)
    if tids:
        tid = tids[0]
        break
print("Token:", tid)

if tid and ex.live:
    from py_clob_client.clob_types import OrderArgs, CreateOrderOptions, OrderType
    args = OrderArgs(token_id=tid, price=0.01, size=5.0, side='BUY', fee_rate_bps=1000)
    opts = CreateOrderOptions(tick_size='0.01', neg_risk=False)
    signed = ex._client.builder.create_order(args, opts)
    try:
        r = ex._client.post_order(signed, OrderType.GTC, post_only=True)
        print("VIA EXECUTOR SUCCESS:", r)
        if isinstance(r, dict) and r.get('orderID'):
            ex._client.cancel(r['orderID'])
    except Exception as e:
        print("VIA EXECUTOR ERROR:", str(e)[:300])

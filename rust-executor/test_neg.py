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
print("Slug:", slug)

resp = requests.get("https://gamma-api.polymarket.com/events/slug/" + slug, timeout=10)
event = resp.json()
for m in event.get('markets', []):
    neg = m.get('negRisk', False)
    cid = m.get('conditionId', '')
    tids = m.get('clobTokenIds', '')
    if isinstance(tids, str):
        tids = json.loads(tids)
    print("gamma negRisk:", neg)
    print("conditionId:", cid[:30])

    resp2 = requests.get("https://clob.polymarket.com/markets/" + cid, timeout=5)
    if resp2.status_code == 200:
        mkt = resp2.json()
        print("CLOB neg_risk:", mkt.get('neg_risk'))
        print("CLOB accepting:", mkt.get('accepting_orders'))

    if tids:
        tid = tids[0]
        print("Token:", tid[:40] + "...")
        # Test with neg_risk=False and True
        for neg_try in [False, True]:
            args = OrderArgs(token_id=tid, price=0.01, size=5.0, side='BUY', fee_rate_bps=1000)
            opts = CreateOrderOptions(tick_size='0.01', neg_risk=neg_try)
            signed = client.builder.create_order(args, opts)
            try:
                r = client.post_order(signed, OrderType.GTC, post_only=True)
                print("  neg_risk=%s: SUCCESS %s" % (neg_try, r))
                if isinstance(r, dict) and r.get('orderID'):
                    client.cancel(r['orderID'])
                break
            except Exception as e:
                print("  neg_risk=%s: %s" % (neg_try, str(e)[:150]))

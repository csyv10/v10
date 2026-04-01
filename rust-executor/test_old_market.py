import os, sys, json, requests
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

# The market that WORKED: btc-updown-5m-1775028000 (07:20)
slug = 'btc-updown-5m-1775028000'
print("Testing old slug:", slug)
resp = requests.get("https://gamma-api.polymarket.com/events/slug/" + slug, timeout=10)
event = resp.json()
for m in event.get('markets', []):
    neg = m.get('negRisk', False)
    cid = m.get('conditionId', '')
    tids = m.get('clobTokenIds', '')
    if isinstance(tids, str):
        tids = json.loads(tids)
    print("  negRisk:", neg, "conditionId:", cid[:30])

    resp2 = requests.get("https://clob.polymarket.com/markets/" + cid, timeout=5)
    if resp2.status_code == 200:
        mkt = resp2.json()
        print("  CLOB neg_risk:", mkt.get('neg_risk'))

    if tids:
        tid = tids[0]
        print("  Token:", tid[:40])

# Test the CURRENT market
slug2 = 'btc-updown-5m-1775028900'
print("\nTesting current slug:", slug2)
resp = requests.get("https://gamma-api.polymarket.com/events/slug/" + slug2, timeout=10)
event = resp.json()
for m in event.get('markets', []):
    neg = m.get('negRisk', False)
    cid = m.get('conditionId', '')
    print("  negRisk:", neg, "conditionId:", cid[:30])

    resp2 = requests.get("https://clob.polymarket.com/markets/" + cid, timeout=5)
    if resp2.status_code == 200:
        mkt = resp2.json()
        print("  CLOB neg_risk:", mkt.get('neg_risk'))
        print("  CLOB exchange:", mkt.get('exchange_address', 'not in response'))

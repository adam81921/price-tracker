#!/usr/bin/env python3
"""華航 TPE-TAK 經濟艙釋位監看（2027/1/23 去 CI178、1/29 回 CI279）。

原理：Google Flights 查「2 大人、經濟艙、來回、只看華航」。
  - 經濟艙有 2 位 → 直飛來回報價 ≈ 2 × 18,200 = 36,400
  - 去程經濟售完      → Google 改報「去程商務＋回程經濟」≈ 49,786
  所以直飛來回價 < THRESHOLD 就代表 1/23 經濟艙釋出 ≥2 位（1 大 1 小一定買得到）。
  ※ Google 對「含兒童」的華航查詢不回報價，所以用 2 大人當代理指標。
資料源：fast-flights（免 API 金鑰）。推播：ntfy（NTFY_TOPIC）。
每次結果寫 data/seatwatch.csv；連續失敗 3 次推播提醒；找到位子後每次都推（有 6 小時冷卻）。
"""
import csv, datetime, json, os, sys, types, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
CSV_PATH = os.path.join(DATA, 'seatwatch.csv')
STATE_PATH = os.path.join(DATA, 'seatwatch_state.json')
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '').strip()
TW = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(TW)

WATCH = dict(id='ci-tpe-tak-0123', origin='TPE', dest='TAK', out='2027-01-23', ret='2027-01-29',
             out_dep='2:30 PM', adults=2, threshold=42000,
             click='https://www.china-airlines.com/tw/zh')
FAIL_ALERT_AFTER = 3
FOUND_COOLDOWN_H = 6


def log(*a):
    print('[seatwatch]', *a, flush=True)


def ntfy(title, body, tags='airplane'):
    if not NTFY_TOPIC:
        log('(NTFY_TOPIC 未設，略過推播)', title, body)
        return
    req = urllib.request.Request('https://ntfy.sh/' + NTFY_TOPIC, data=body.encode(), method='POST')
    req.add_header('Title', title.encode()); req.add_header('Tags', tags)
    req.add_header('Priority', 'high'); req.add_header('Click', WATCH['click'])
    urllib.request.urlopen(req, timeout=30)
    log('已推播:', title)


def fetch_direct_price():
    """回傳 (price:int|None, note:str)。price=None 表示 Google 沒列出華航直飛。"""
    stub = types.ModuleType('fast_flights.fallback_playwright')
    stub.fallback_playwright_fetch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no playwright'))
    sys.modules['fast_flights.fallback_playwright'] = stub
    from fast_flights import FlightData, Passengers, get_flights
    r = get_flights(
        flight_data=[FlightData(date=WATCH['out'], from_airport=WATCH['origin'], to_airport=WATCH['dest']),
                     FlightData(date=WATCH['ret'], from_airport=WATCH['dest'], to_airport=WATCH['origin'])],
        trip='round-trip', seat='economy', passengers=Passengers(adults=WATCH['adults']), fetch_mode='common')
    for f in r.flights:
        if f.name == 'China Airlines' and f.stops == 0 and f.departure.startswith(WATCH['out_dep']):
            digits = ''.join(ch for ch in f.price if ch.isdigit())
            return (int(digits) if digits else None), f'{f.name} {f.departure} {f.price}'
    return None, f'no CI direct among {len(r.flights)} results'


def main():
    os.makedirs(DATA, exist_ok=True)
    try:
        state = json.load(open(STATE_PATH))
    except Exception:
        state = {}
    ts = NOW.strftime('%Y-%m-%d %H:%M')
    try:
        price, note = fetch_direct_price()
        err = ''
    except Exception as e:
        price, note, err = None, '', f'{type(e).__name__}: {e}'
    log(ts, 'price=', price, note, err)

    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'id', 'price_2adults_rt', 'note', 'error'])
        w.writerow([ts, WATCH['id'], price if price is not None else '', note, err])

    if err:
        state['fails'] = state.get('fails', 0) + 1
        if state['fails'] == FAIL_ALERT_AFTER:
            ntfy('⚠️ 高松釋位監看連續失敗', f'{FAIL_ALERT_AFTER} 次抓不到 Google 航班資料，最後錯誤：{err[:150]}', tags='warning')
    else:
        state['fails'] = 0
        state['last_price'] = price
        state['last_ok'] = ts
        if price is not None and price < WATCH['threshold']:
            last = state.get('last_found_alert')
            cool = last and (NOW - datetime.datetime.fromisoformat(last)).total_seconds() < FOUND_COOLDOWN_H * 3600
            if not cool:
                per = price // 2
                ntfy('✈️ 1/23 華航高松經濟艙有位了！',
                     f'Google 顯示 2 大人來回經濟艙 {price:,}（每人約 {per:,}），1/23 CI178 去程經濟艙已釋出 ≥2 位，'
                     f'快去華航 App/官網用「1 大 1 小」下單。', tags='tada')
                state['last_found_alert'] = NOW.isoformat()
    json.dump(state, open(STATE_PATH, 'w'), ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""華航 TPE-TAK 經濟艙釋位監看（2027/1/23 去 CI178、1/29 回 CI279）。

原理：Google Flights 查「2 大人、經濟艙、來回、只看華航」。
  - 經濟艙有 2 位 → 直飛來回報價 ≈ 2 × 18,200 = 36,400
  - 去程經濟售完      → Google 改報「去程商務＋回程經濟」≈ 49,786
  所以直飛來回價 < THRESHOLD 就代表 1/23 經濟艙釋出 ≥2 位（1 大 1 小一定買得到）。
  ※ Google 對「含兒童」的華航查詢不回報價，所以用 2 大人當代理指標。
資料源：fast-flights（免 API 金鑰）。**必須在台灣 IP 跑**（GitHub Actions 在美國：Google 回美國市場
  的舊快取＋USD，實測會誤報），所以由本機 LaunchAgent `com.adam.seatwatch` 每 4 小時執行。
推播：本機沒有 ntfy topic → `gh workflow run notify.yml` 讓 GitHub 用 secret 發 ntfy；另外 macOS 通知一份。
每次結果寫 data/seatwatch_local.csv（不進 git）。**每次執行都推一則**（使用者要求，才能分辨「沒票」和「推播壞掉」）：
  有位＝high 優先（響鈴）／仍售完＝low 優先（靜音）／抓取失敗＝default。
"""
import csv, datetime, json, os, subprocess, sys, types

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
CSV_PATH = os.path.join(DATA, 'seatwatch_local.csv')
STATE_PATH = os.path.join(DATA, 'seatwatch_state.json')
REPO = 'adam81921/price-tracker'
GH = '/opt/homebrew/bin/gh'
TW = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(TW)

WATCH = dict(id='ci-tpe-tak-0123', origin='TPE', dest='TAK', out='2027-01-23', ret='2027-01-29',
             out_dep='2:30 PM', adults=2, threshold=42000,
             click='https://www.china-airlines.com/tw/zh')
FAIL_ALERT_AFTER = 3
FOUND_COOLDOWN_H = 6


def log(*a):
    print('[seatwatch]', *a, flush=True)


def ntfy(title, body, tags='airplane', priority='high'):
    """透過 GitHub notify.yml 轉發 ntfy（secret 在 repo），並發 macOS 通知。"""
    try:
        subprocess.run(['osascript', '-e', f'display notification "{body[:200]}" with title "{title}" sound name "Glass"'],
                       timeout=15, check=False)
    except Exception as e:
        log('macOS 通知失敗:', e)
    r = subprocess.run([GH, 'workflow', 'run', 'notify.yml', '-R', REPO,
                        '-f', f'title={title}', '-f', f'body={body}', '-f', f'click={WATCH["click"]}', '-f', f'tags={tags}', '-f', f'priority={priority}'],
                       capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        log('已推播(via GitHub):', title)
    else:
        log('推播失敗:', r.stderr.strip()[:200])


def fetch_direct_price():
    """回傳 (price:int|None, note:str)。price=None 表示 Google 沒列出華航直飛。"""
    stub = types.ModuleType('fast_flights.fallback_playwright')
    stub.fallback_playwright_fetch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no playwright'))
    sys.modules['fast_flights.fallback_playwright'] = stub
    from fast_flights import FlightData, Passengers, TFSData, get_flights_from_filter
    tfs = TFSData.from_interface(
        flight_data=[FlightData(date=WATCH['out'], from_airport=WATCH['origin'], to_airport=WATCH['dest']),
                     FlightData(date=WATCH['ret'], from_airport=WATCH['dest'], to_airport=WATCH['origin'])],
        trip='round-trip', seat='economy', passengers=Passengers(adults=WATCH['adults']), max_stops=None)
    r = get_flights_from_filter(tfs, currency='TWD', mode='common')   # GitHub Actions 在美國，不指定會回 USD
    for f in r.flights:
        if f.name == 'China Airlines' and f.stops == 0 and f.departure.startswith(WATCH['out_dep']):
            if 'NT$' not in f.price:
                raise RuntimeError(f'幣別不是 TWD: {f.price}')
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
        ntfy('⚠️ 高松釋位監看抓取失敗', f'第 {state["fails"]} 次連續失敗：{err[:150]}', tags='warning', priority='default')
    else:
        state['fails'] = 0
        state['last_price'] = price
        state['last_ok'] = ts
        if price is not None and price < WATCH['threshold']:
            per = price // 2
            ntfy('✈️ 1/23 華航高松經濟艙有位了！',
                 f'Google 顯示 2 大人來回經濟艙 {price:,}（每人約 {per:,}），1/23 CI178 去程經濟艙已釋出 ≥2 位，'
                 f'快去華航 App 用「1 大 1 小」下單。', tags='tada', priority='high')
            state['last_found_alert'] = NOW.isoformat()
        elif price is None:
            ntfy('⚠️ 高松釋位監看：Google 沒列出華航直飛', note[:150], tags='warning', priority='default')
        else:
            ntfy('😴 1/23 高松經濟艙仍售完', f'{ts} 查：2 大人來回 {price:,}（去程仍商務）。下次 4 小時後。',
                 tags='zzz', priority='low')
    json.dump(state, open(STATE_PATH, 'w'), ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""通用價格追蹤器：機票（特定航班）＋飯店（特定日期/房型）。

資料來源：
  - SerpAPI Google Flights / Google Hotels（需 SERPAPI_KEY，免費 100 查詢/月，
    用 state.json 記帳控制在 serpapi_monthly_budget 內）
  - 樂天 Travel VacantHotelSearch（需 RAKUTEN_APP_ID，免費，有房型/方案名 → room_keyword 過濾）

推播：ntfy.sh（需 NTFY_TOPIC）。觸發：低於 threshold／30 天新低／單日跌幅 ≥ drop_alert_pct%。
所有價格寫入 data/prices.csv（date,id,source,label,price,currency）。
設計原則：單筆 watch 失敗不影響其他筆，整體 exit 0（讓 GitHub Actions 照常 commit 資料）。
"""
import csv
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
CSV_PATH = os.path.join(DATA, 'prices.csv')
STATE_PATH = os.path.join(DATA, 'state.json')

SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '').strip()
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '').strip()
RAKUTEN_APP_ID = os.environ.get('RAKUTEN_APP_ID', '').strip()

# 以台北時區為「今天」（GitHub Actions 跑在 UTC）
TW = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TW).strftime('%Y-%m-%d')
THIS_MONTH = TODAY[:7]


def log(*args):
    print('[tracker]', *args, flush=True)


def http_json(url, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'price-tracker/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------- state / csv ----------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(DATA, exist_ok=True)
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_rows(rows):
    os.makedirs(DATA, exist_ok=True)
    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, 'a', newline='') as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(['date', 'id', 'source', 'label', 'price', 'currency'])
        for r in rows:
            w.writerow(r)


def history_min_by_day(watch_id, days):
    """回傳 {date: 當日最低價}，取最近 days 天。"""
    out = {}
    if not os.path.exists(CSV_PATH):
        return out
    cutoff = (datetime.datetime.now(TW) - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            if row['id'] != watch_id or row['date'] < cutoff:
                continue
            try:
                p = float(row['price'])
            except ValueError:
                continue
            d = row['date']
            if d not in out or p < out[d]:
                out[d] = p
    return out


# ---------------- serpapi ----------------

def serpapi_budget_left(state, settings):
    used = state.get('serpapi_calls', {}).get(THIS_MONTH, 0)
    return settings.get('serpapi_monthly_budget', 95) - used


def serpapi(state, params):
    params = dict(params)
    params['api_key'] = SERPAPI_KEY
    url = 'https://serpapi.com/search.json?' + urllib.parse.urlencode(params)
    calls = state.setdefault('serpapi_calls', {})
    calls[THIS_MONTH] = calls.get(THIS_MONTH, 0) + 1
    return http_json(url)


def fetch_flight(state, w):
    """回傳 [(source, label, price)]。include_airlines 一定要在 API 層設，
    否則會撈到「去程長榮＋回程他航」的假低價（舊 flight-tracker 教訓）。"""
    params = {
        'engine': 'google_flights',
        'departure_id': w['origin'],
        'arrival_id': w['dest'],
        'outbound_date': w['outbound_date'],
        'currency': w.get('currency', 'TWD'),
        'hl': 'zh-tw',
    }
    if w.get('return_date'):
        params['type'] = '1'
        params['return_date'] = w['return_date']
    else:
        params['type'] = '2'
    if w.get('include_airlines'):
        params['include_airlines'] = w['include_airlines']
    # 乘客數（預設 1 大人）；門檻即針對此組合的合計票價
    for pk in ('adults', 'children', 'infants_in_seat', 'infants_on_lap'):
        if w.get(pk):
            params[pk] = str(w[pk])
    data = serpapi(state, params)
    if 'error' in data:
        raise RuntimeError('serpapi flights: ' + str(data['error']))

    want = {x.replace(' ', '').upper() for x in w.get('flight_numbers', [])}
    rows = []
    for f in (data.get('best_flights') or []) + (data.get('other_flights') or []):
        nums = [s.get('flight_number', '').replace(' ', '').upper() for s in f.get('flights', [])]
        price = f.get('price')
        if price is None:
            continue
        # flight_numbers 有指定時，行程中至少要含其中一班（去程搜尋結果的去程段）
        if want and not (want & set(nums)):
            continue
        rows.append(('google_flights', '+'.join(nums) or 'itinerary', float(price)))
    if rows:
        rows.sort(key=lambda r: r[2])
        return [rows[0]]  # 只記最低的那個符合行程
    return []


def fetch_hotel_google(state, w):
    """回傳 [(source, label, price_per_night)]；property_token 快取在 state 省一次查詢。"""
    base = {
        'engine': 'google_hotels',
        'q': w['query'],
        'check_in_date': w['check_in'],
        'check_out_date': w['check_out'],
        'adults': str(w.get('adults', 2)),
        'currency': w.get('currency', 'JPY'),
        'hl': 'zh-tw',
        'gl': 'jp',
    }
    tokens = state.setdefault('property_tokens', {})
    token = tokens.get(w['query'])
    if not token:
        data = serpapi(state, base)
        if 'error' in data:
            raise RuntimeError('serpapi hotels search: ' + str(data['error']))
        props = data.get('properties') or []
        if not props:
            raise RuntimeError('google_hotels 查無 "%s"' % w['query'])
        token = props[0].get('property_token')
        tokens[w['query']] = token
        log('  property_token cached for', w['query'], '->', (props[0].get('name') or '?'))

    detail = serpapi(state, dict(base, property_token=token))
    if 'error' in detail:
        tokens.pop(w['query'], None)  # token 可能過期，下次重查
        raise RuntimeError('serpapi hotels detail: ' + str(detail['error']))

    rows = []
    kw = w.get('room_keyword') or ''
    # featured_prices 有房型明細（不是每家 OTA 都給）
    for fp in detail.get('featured_prices') or []:
        src = fp.get('source') or 'OTA'
        for room in fp.get('rooms') or []:
            name = room.get('name') or ''
            rate = ((room.get('rate_per_night') or {}).get('extracted_lowest')
                    or (room.get('rate_per_night') or {}).get('extracted_before_taxes_fees'))
            if rate is None:
                continue
            if kw and not re.search(kw, name, re.I):
                continue
            rows.append(('google:' + src, name[:60] or 'room', float(rate)))
    # prices = 各 OTA 整體最低價（無房型）；有指定 room_keyword 時不混入
    if not kw:
        for p in detail.get('prices') or []:
            src = p.get('source') or 'OTA'
            rate = ((p.get('rate_per_night') or {}).get('extracted_lowest')
                    or (p.get('rate_per_night') or {}).get('extracted_before_taxes_fees'))
            if rate is not None:
                rows.append(('google:' + src, 'lowest', float(rate)))
    if rows:
        rows.sort(key=lambda r: r[2])
        return rows[:3]  # 記最便宜的前三個來源
    return []


# ---------------- rakuten ----------------

def rakuten_resolve_hotel_no(state, w):
    cache = state.setdefault('rakuten_hotel_nos', {})
    if w.get('rakuten_hotel_no'):
        return w['rakuten_hotel_no']
    if w['query'] in cache:
        return cache[w['query']]
    url = ('https://app.rakuten.co.jp/services/api/Travel/KeywordHotelSearch/20170426?'
           + urllib.parse.urlencode({
               'applicationId': RAKUTEN_APP_ID, 'format': 'json',
               'keyword': re.sub(r'[A-Za-z ]+$', '', w['query']).strip() or w['query'],
               'hits': 3}))
    data = http_json(url)
    hotels = data.get('hotels') or []
    if not hotels:
        return None
    info = hotels[0]['hotel'][0]['hotelBasicInfo']
    cache[w['query']] = info['hotelNo']
    log('  rakuten hotelNo cached for', w['query'], '->', info.get('hotelName', '?'), info['hotelNo'])
    return info['hotelNo']


def fetch_hotel_rakuten(state, w):
    """樂天空房搜尋：有 roomName/planName → 可做 room_keyword 過濾。價格=整段住宿總額/晚數。"""
    hotel_no = rakuten_resolve_hotel_no(state, w)
    if not hotel_no:
        raise RuntimeError('樂天查無 "%s"（可在 watchlist 手動填 rakuten_hotel_no）' % w['query'])
    nights = (datetime.date.fromisoformat(w['check_out']) - datetime.date.fromisoformat(w['check_in'])).days or 1
    url = ('https://app.rakuten.co.jp/services/api/Travel/VacantHotelSearch/20170426?'
           + urllib.parse.urlencode({
               'applicationId': RAKUTEN_APP_ID, 'format': 'json',
               'hotelNo': hotel_no,
               'checkinDate': w['check_in'], 'checkoutDate': w['check_out'],
               'adultNum': w.get('adults', 2)}))
    try:
        data = http_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # 樂天上已無空房
            return [('rakuten', 'no_vacancy', float('nan'))]
        raise

    kw = w.get('room_keyword') or ''
    rows = []
    for h in data.get('hotels') or []:
        for part in h.get('hotel') or []:
            ri = part.get('roomInfo')
            if not ri:
                continue
            basic, charge = {}, {}
            for x in ri:
                basic.update(x.get('roomBasicInfo') or {})
                charge.update(x.get('dailyCharge') or {})
            name = ' '.join(filter(None, [basic.get('roomName'), basic.get('planName')]))
            total = charge.get('total')
            if total is None:
                continue
            if kw and not re.search(kw, name, re.I):
                continue
            rows.append(('rakuten', name[:60] or 'plan', float(total) / nights))
    if rows:
        rows.sort(key=lambda r: r[2])
        return rows[:3]
    return []


# ---------------- alerts ----------------

def ntfy(title, body, click=None, tags='moneybag'):
    if not NTFY_TOPIC:
        log('  (NTFY_TOPIC 未設，略過推播)', title)
        return
    headers = {'Title': title.encode('utf-8'), 'Tags': tags, 'Priority': 'high'}
    if click:
        headers['Click'] = click
    req = urllib.request.Request('https://ntfy.sh/' + NTFY_TOPIC,
                                 data=body.encode('utf-8'), method='POST')
    for k, v in headers.items():
        req.add_header(k, v)
    urllib.request.urlopen(req, timeout=30)
    log('  📣 已推播:', title)


def check_alerts(state, settings, w, today_rows):
    prices = [p for _, _, p in today_rows if p == p]  # 排除 NaN
    if not prices:
        return
    best_src, best_label, best = min(today_rows, key=lambda r: (r[2] if r[2] == r[2] else 1e18))
    cur = w.get('currency', 'JPY')
    reasons = []

    if w.get('threshold') and best <= w['threshold']:
        reasons.append('低於門檻 %s' % f"{w['threshold']:,.0f}")

    hist = history_min_by_day(w['id'], 30)
    past = [v for d, v in hist.items() if d < TODAY]
    if settings.get('low30_alert', True) and len(past) >= 5 and best < min(past):
        reasons.append('30天新低（原低點 %s）' % f'{min(past):,.0f}')
    yesterday = (datetime.datetime.now(TW) - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    if yesterday in hist and hist[yesterday] > 0:
        drop = (hist[yesterday] - best) / hist[yesterday] * 100
        if drop >= settings.get('drop_alert_pct', 5):
            reasons.append('單日下跌 %.1f%%' % drop)

    if not reasons:
        return

    # 冷卻：同一 watch 同價位帶 N 天內不重複吵
    la = state.setdefault('last_alerts', {}).get(w['id'])
    if la:
        days_since = (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(la['date'])).days
        if days_since < settings.get('alert_cooldown_days', 3) and best >= la['price'] * 0.98:
            log('  (冷卻中，略過推播)')
            return

    title = '💰 %s %s %s' % (w['id'], f'{best:,.0f}', cur)
    body = '%s\n%s（%s）\n%s' % (w.get('note', ''), best_label, best_src, '；'.join(reasons))
    ntfy(title, body.strip(), click=w.get('url'))
    state['last_alerts'][w['id']] = {'date': TODAY, 'price': best}


# ---------------- main ----------------

def due_for_serpapi(state, w):
    every = w.get('every_n_days', 1)
    last = state.get('last_serpapi_run', {}).get(w['id'])
    if not last:
        return True
    return (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(last)).days >= every


def main():
    with open(os.path.join(ROOT, 'watchlist.json')) as f:
        wl = json.load(f)
    settings = wl.get('settings', {})
    state = load_state()
    all_rows = []

    for w in wl.get('watches', []):
        if not w.get('enabled'):
            continue
        log('==', w['id'])
        rows = []
        try:
            if w['type'] == 'flight':
                if not SERPAPI_KEY:
                    log('  SERPAPI_KEY 未設，略過')
                elif serpapi_budget_left(state, settings) < 1:
                    log('  ⚠️ SerpAPI 本月額度用完，略過')
                elif due_for_serpapi(state, w):
                    rows += fetch_flight(state, w)
                    state.setdefault('last_serpapi_run', {})[w['id']] = TODAY
                else:
                    log('  未到查詢日（every_n_days=%s）' % w.get('every_n_days', 1))

            elif w['type'] == 'hotel':
                if RAKUTEN_APP_ID:  # 樂天免費，每天查
                    try:
                        rows += fetch_hotel_rakuten(state, w)
                    except Exception as e:
                        log('  rakuten 失敗:', e)
                if not SERPAPI_KEY:
                    log('  SERPAPI_KEY 未設，略過 google_hotels')
                elif serpapi_budget_left(state, settings) < 2:
                    log('  ⚠️ SerpAPI 額度不足（留 buffer），略過 google_hotels')
                elif due_for_serpapi(state, w):
                    rows += fetch_hotel_google(state, w)
                    state.setdefault('last_serpapi_run', {})[w['id']] = TODAY
                else:
                    log('  google_hotels 未到查詢日（every_n_days=%s）' % w.get('every_n_days', 1))
        except Exception as e:
            log('  ❌', e)

        for src, label, price in rows:
            log('  %-22s %-40s %s' % (src, label, f'{price:,.0f}' if price == price else 'no_vacancy'))
        all_rows += [[TODAY, w['id'], src, label.replace(',', '，'),
                      ('%.0f' % price) if price == price else '', w.get('currency', 'JPY')]
                     for src, label, price in rows]
        if rows:
            try:
                check_alerts(state, settings, w, rows)
            except Exception as e:
                log('  alert 失敗:', e)

        # 每日現價推播（notify_daily）：不受門檻/冷卻限制，到 notify_daily_until 自動停
        if rows and w.get('notify_daily'):
            until = w.get('notify_daily_until')
            if not until or TODAY <= until:
                try:
                    _, dl, dp = min(rows, key=lambda r: (r[2] if r[2] == r[2] else 1e18))
                    body = '%s\n%s' % (w.get('note', ''), dl)
                    if until and TODAY == until:
                        body += '\n⏰ 今天是每日推播最後一天，回顧一週趨勢決定追蹤門檻。'
                    ntfy('📊 今日 %s %s %s' % (w['id'], f'{dp:,.0f}', w.get('currency', 'TWD')),
                         body, tags='chart_with_upwards_trend')
                except Exception as e:
                    log('  daily push 失敗:', e)

    if all_rows:
        append_rows(all_rows)
        log('已寫入 %d 筆 -> data/prices.csv' % len(all_rows))
    used = state.get('serpapi_calls', {}).get(THIS_MONTH, 0)
    log('SerpAPI 本月已用 %d / %d' % (used, settings.get('serpapi_monthly_budget', 95)))
    save_state(state)


if __name__ == '__main__':
    main()

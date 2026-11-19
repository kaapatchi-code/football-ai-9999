import os, math, json, time, threading, re, unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
ALLOWED_ORIGINS = [x.strip() for x in os.getenv('CORS_ORIGINS', '*').split(',') if x.strip()]

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin', '')
    if '*' in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = '*'
    elif origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/api/<path:_path>', methods=['OPTIONS'])
def api_options(_path):
    return ('', 204)

API_KEY = os.getenv('HIGHLIGHTLY_API_KEY', '').strip()
API_HOST = os.getenv('HIGHLIGHTLY_BASE_URL', 'https://soccer.highlightly.net').rstrip('/')
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_DAILY_API_CALLS = 0
_DAILY_USAGE_DATE = datetime.now(timezone.utc).date()
_DAILY_API_SOFT_LIMIT = int(os.getenv('API_DAILY_SOFT_LIMIT', '85'))
_LAST_REMAINING = None
_PROVIDER_QUOTA_EXHAUSTED = False
_ENABLE_DETAILED_MATCH_STATS = os.getenv('ENABLE_DETAILED_MATCH_STATS', 'false').strip().lower() in ('1','true','yes','on')


def cache_ttl(path):
    if path.startswith('/teams?'): return 24 * 60 * 60
    if path.startswith('/last-five-games'): return 60 * 60
    if path.startswith('/matches/'): return 24 * 60 * 60
    if path.startswith('/statistics/'): return 24 * 60 * 60
    return 10 * 60


def api_get(path, params=None, cache_seconds=None):
    global _DAILY_USAGE_DATE, _DAILY_API_CALLS, _LAST_REMAINING, _PROVIDER_QUOTA_EXHAUSTED
    if not API_KEY:
        raise RuntimeError('مفتاح Highlightly غير مضبوط. أضف HIGHLIGHTLY_API_KEY في إعدادات Render.')
    params = params or {}
    cache_key = path + '?' + json.dumps(params, sort_keys=True, ensure_ascii=False)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        ttl = cache_ttl(path) if cache_seconds is None else max(0, int(cache_seconds))
        if ttl and cached and now - cached['time'] < ttl:
            return cached['data']
        today = datetime.now(timezone.utc).date()
        if today != _DAILY_USAGE_DATE:
            _DAILY_USAGE_DATE = today
            _DAILY_API_CALLS = 0
            _LAST_REMAINING = None
            _PROVIDER_QUOTA_EXHAUSTED = False
        if _PROVIDER_QUOTA_EXHAUSTED:
            raise RuntimeError('انتهت حصة Highlightly اليومية. أوقفنا الطلبات مؤقتاً لتجنب استهلاك طلبات مرفوضة؛ ستُعاد المحاولة بعد تجدد الحصة اليومية.')
        if _DAILY_API_CALLS >= _DAILY_API_SOFT_LIMIT:
            raise RuntimeError(f'وصل الموقع إلى حد الحماية اليومي ({_DAILY_API_SOFT_LIMIT} طلباً). انتظر حتى تتجدد حصة Highlightly.')
        _DAILY_API_CALLS += 1
    try:
        response = requests.get(API_HOST + path,
            headers={'x-rapidapi-key': API_KEY}, params=params, timeout=20)
        remaining = response.headers.get('x-ratelimit-requests-remaining')
        if remaining is not None:
            try: _LAST_REMAINING = int(remaining)
            except ValueError: pass
        if response.status_code == 429:
            with _CACHE_LOCK:
                _PROVIDER_QUOTA_EXHAUSTED = True
            raise RuntimeError('انتهت حصة Highlightly أو تم بلوغ حد الطلبات (429). أوقفنا الطلبات الجديدة مؤقتاً حتى تجدد الحصة اليومية. راجع لوحة Highlightly.')
        if response.status_code in (401, 403):
            raise RuntimeError('Highlightly رفض المفتاح أو صلاحية الحساب. تحقق من المفتاح والخطة في لوحة Highlightly.')
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError('تعذر الاتصال بـ Highlightly: ' + str(exc)) from exc
    if isinstance(payload, dict) and payload.get('error'):
        raise RuntimeError(str(payload['error']))
    if cache_seconds != 0:
        with _CACHE_LOCK:
            _CACHE[cache_key] = {'time': now, 'data': payload}
    return payload


def payload_data(payload):
    if isinstance(payload, dict) and isinstance(payload.get('data'), list):
        return payload['data']
    if isinstance(payload, list):
        return payload
    return []


def first_record(payload):
    if isinstance(payload, list): return payload[0] if payload else {}
    if isinstance(payload, dict) and isinstance(payload.get('data'), list):
        return payload['data'][0] if payload['data'] else {}
    return payload if isinstance(payload, dict) else {}


def safe_mean(values):
    return round(sum(values)/len(values), 2) if values else None


def parse_score(score):
    if not score: return (None, None)
    match = re.search(r'(\d+)\s*[-:]\s*(\d+)', str(score))
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def parse_minute(value):
    if value is None: return None
    match = re.match(r'\s*(\d+)', str(value))
    return int(match.group(1)) if match else None


def get_team_last_five(team_id):
    return payload_data(api_get('/last-five-games', {'teamId': int(team_id)}))[:5]


def match_details(match_id):
    return first_record(api_get(f'/matches/{int(match_id)}'))


def stats_for_fixtures(fixtures):
    """Normalize Highlightly's last-five response and detailed match statistics."""
    out = []
    for item in fixtures:
        home = item.get('homeTeam') or {}
        away = item.get('awayTeam') or {}
        state = item.get('state') or {}
        score = state.get('score') or {}
        hg, ag = parse_score(score.get('current'))
        fx = {
            'fixture_id': item.get('id'), 'date': item.get('date'),
            'timestamp': None, 'home_id': home.get('id'), 'away_id': away.get('id'),
            'home_name': home.get('name'), 'away_name': away.get('name'),
            'home_goals': hg, 'away_goals': ag, 'ht_home': None, 'ht_away': None,
            'ft_home': hg, 'ft_away': ag,
            'status': 'FT' if str(state.get('description','')).lower().startswith('finished') else state.get('description','')
        }
        # Optional detailed requests can add xG/corners/cards/half-time data, but cost up to 5 extra API calls per team.
        # Disabled by default on the free plan; enable only when quota allows.
        if fx['fixture_id'] and _ENABLE_DETAILED_MATCH_STATS:
            try:
                detail = match_details(fx['fixture_id'])
                for event in detail.get('events') or []:
                    minute = parse_minute(event.get('time'))
                    if minute is None or minute > 45: continue
                    event_type = str(event.get('type') or '').lower()
                    if event_type not in ('goal', 'own goal', 'penalty'): continue
                    tid = (event.get('team') or {}).get('id')
                    if tid == fx['home_id']: fx['ht_home'] = (fx['ht_home'] or 0) + 1
                    elif tid == fx['away_id']: fx['ht_away'] = (fx['ht_away'] or 0) + 1
                # If the match had no first-half goals, an available event list means 0-0 at half-time.
                if isinstance(detail.get('events'), list):
                    fx['ht_home'] = fx['ht_home'] or 0
                    fx['ht_away'] = fx['ht_away'] or 0
                fx['_statistics'] = detail.get('statistics') or []
            except Exception as exc:
                fx['_statistics'] = []
                fx['_detail_note'] = str(exc)[:100]
        elif fx['fixture_id']:
            fx['_detail_note'] = 'تم إيقاف التفاصيل الموسعة لتوفير حصة API المجانية.'
            fx['_statistics'] = []
        out.append(fx)
    return out


def team_metrics(team_id, fixtures):
    played = [x for x in fixtures if str(x['status']).lower().startswith('ft') and x['home_goals'] is not None and x['away_goals'] is not None]
    gf=[]; ga=[]; outcomes=[]; btts=[]; o15=[]; o25=[]; o35=[]; clean=[]; fts=[]; ht_g=[]; ht_con=[]; st_g=[]; st_con=[]; comebacks=[]
    home=[]; away=[]
    valid_played=[]
    for x in played:
        # Never assume the requested team is the away side when IDs do not match.
        # That fallback caused false W/L labels when a provider record was malformed.
        home_match = x.get('home_id') is not None and str(x.get('home_id')) == str(team_id)
        away_match = x.get('away_id') is not None and str(x.get('away_id')) == str(team_id)
        if not home_match and not away_match:
            continue
        valid_played.append(x)
        is_home = home_match
        scored = x['home_goals'] if is_home else x['away_goals']
        conceded = x['away_goals'] if is_home else x['home_goals']
        gf.append(scored); ga.append(conceded)
        outcomes.append('W' if scored>conceded else 'D' if scored==conceded else 'L')
        btts.append(int(scored>0 and conceded>0)); total=scored+conceded
        o15.append(int(total>=2)); o25.append(int(total>=3)); o35.append(int(total>=4))
        clean.append(int(conceded==0)); fts.append(int(scored==0))
        if x['ht_home'] is not None and x['ht_away'] is not None:
            hts = x['ht_home'] if is_home else x['ht_away']; htc = x['ht_away'] if is_home else x['ht_home']
            ht_g.append(hts); ht_con.append(htc)
            st_g.append(max(0,scored-hts)); st_con.append(max(0,conceded-htc))
            comebacks.append(1 if hts < htc and scored > conceded else 0)
        if is_home: home.append((scored,conceded))
        else: away.append((scored,conceded))
    return {
        'played':len(outcomes),'form':outcomes,'wins':outcomes.count('W'),'draws':outcomes.count('D'),'losses':outcomes.count('L'),
        'gf':gf,'ga':ga,'avg_gf':safe_mean(gf),'avg_ga':safe_mean(ga),
        'btts_pct':round(100*sum(btts)/len(btts),1) if btts else None,
        'over15_pct':round(100*sum(o15)/len(o15),1) if o15 else None,
        'over25_pct':round(100*sum(o25)/len(o25),1) if o25 else None,
        'over35_pct':round(100*sum(o35)/len(o35),1) if o35 else None,
        'clean_pct':round(100*sum(clean)/len(clean),1) if clean else None,
        'fts_pct':round(100*sum(fts)/len(fts),1) if fts else None,
        'ht_avg_gf':safe_mean(ht_g),'ht_avg_ga':safe_mean(ht_con),'ht_sample':len(ht_g),
        'st_avg_gf':safe_mean(st_g),'st_avg_ga':safe_mean(st_con),
        'comebacks':sum(comebacks) if comebacks else (0 if valid_played and len(ht_g)==len(valid_played) else None),
        'home_avg_gf':safe_mean([a for a,b in home]),'home_avg_ga':safe_mean([b for a,b in home]),'home_played':len(home),
        'away_avg_gf':safe_mean([a for a,b in away]),'away_avg_ga':safe_mean([b for a,b in away]),'away_played':len(away),
        'last_fixtures':valid_played[:5]
    }



def advanced_team_metrics(team_id, fixtures):
    """Thirty extra indicators derived only from the team's available completed matches.
    Missing half-time data remains unavailable; no external or guessed values are inserted.
    """
    rows=[]
    for x in fixtures:
        if not str(x.get('status','')).lower().startswith('ft') or x.get('home_goals') is None or x.get('away_goals') is None:
            continue
        is_home = str(x.get('home_id')) == str(team_id)
        is_away = str(x.get('away_id')) == str(team_id)
        if not (is_home or is_away):
            continue
        gf = x['home_goals'] if is_home else x['away_goals']
        ga = x['away_goals'] if is_home else x['home_goals']
        hg = x.get('ht_home') if is_home else x.get('ht_away')
        hga = x.get('ht_away') if is_home else x.get('ht_home')
        result = 'W' if gf > ga else ('D' if gf == ga else 'L')
        points = 3 if result == 'W' else (1 if result == 'D' else 0)
        rows.append({'gf':gf,'ga':ga,'total':gf+ga,'gd':gf-ga,'result':result,'points':points,
                     'home':is_home,'ht_gf':hg,'ht_ga':hga})
    n=len(rows)
    def mean(vals): return sum(vals)/len(vals) if vals else None
    def pct(test, source=None):
        source = rows if source is None else source
        return (100*sum(1 for r in source if test(r))/len(source)) if source else None
    def fmt(v, suffix=''):
        return 'غير متاح' if v is None else f'{v:.2f}{suffix}'
    def fmt_pct(v): return 'غير متاح' if v is None else f'{v:.1f}%'
    def std(vals):
        if len(vals)<2: return None
        avg=mean(vals)
        return math.sqrt(sum((v-avg)**2 for v in vals)/len(vals))
    ht_rows=[r for r in rows if r['ht_gf'] is not None and r['ht_ga'] is not None]
    last3=rows[:3]
    prev2=rows[3:5]
    last3_ppg=mean([r['points'] for r in last3])
    prev2_ppg=mean([r['points'] for r in prev2])
    delta=(last3_ppg-prev2_ppg) if last3_ppg is not None and prev2_ppg is not None else None
    metrics=[
      ('points_per_game','النقاط لكل مباراة',fmt(mean([r['points'] for r in rows])),'متوسط النقاط وفق نظام 3 للفوز و1 للتعادل.'),
      ('goal_difference_total','فارق الأهداف الإجمالي',fmt(sum(r['gd'] for r in rows)),'الأهداف المسجلة ناقص المستقبلة في العينة.'),
      ('avg_goal_difference','متوسط فارق الأهداف',fmt(mean([r['gd'] for r in rows])),'متوسط فارق الأهداف في المباراة.'),
      ('avg_total_goals','متوسط إجمالي أهداف المباراة',fmt(mean([r['total'] for r in rows])),'أهداف الفريقين معاً في كل مباراة.'),
      ('scoring_stddev','تذبذب تسجيل الفريق',fmt(std([r['gf'] for r in rows])),'الانحراف المعياري لأهداف الفريق؛ الأقل يعني تسجيلًا أكثر ثباتاً ضمن العينة.'),
      ('conceding_stddev','تذبذب استقبال الفريق',fmt(std([r['ga'] for r in rows])),'الانحراف المعياري للأهداف المستقبلة.'),
      ('total_goals_stddev','تذبذب إجمالي الأهداف',fmt(std([r['total'] for r in rows])),'الانحراف المعياري لمجموع أهداف المباراة.'),
      ('scored_1plus_pct','سجل هدفاً واحداً على الأقل',fmt_pct(pct(lambda r:r['gf']>=1)),'نسبة المباريات التي سجل فيها الفريق.'),
      ('scored_2plus_pct','سجل هدفين أو أكثر',fmt_pct(pct(lambda r:r['gf']>=2)),'نسبة المباريات التي سجل فيها الفريق هدفين على الأقل.'),
      ('scored_3plus_pct','سجل 3 أهداف أو أكثر',fmt_pct(pct(lambda r:r['gf']>=3)),'نسبة المباريات ذات التسجيل المرتفع للفريق.'),
      ('conceded_2plus_pct','استقبل هدفين أو أكثر',fmt_pct(pct(lambda r:r['ga']>=2)),'نسبة المباريات التي استقبل فيها هدفين على الأقل.'),
      ('conceded_3plus_pct','استقبل 3 أهداف أو أكثر',fmt_pct(pct(lambda r:r['ga']>=3)),'نسبة المباريات التي استقبل فيها ثلاثة أهداف على الأقل.'),
      ('unbeaten_pct','نسبة المباريات دون خسارة',fmt_pct(pct(lambda r:r['result']!='L')),'الفوز أو التعادل ضمن العينة.'),
      ('win_margin_2plus_pct','فوز بفارق هدفين أو أكثر',fmt_pct(pct(lambda r:r['gd']>=2)),'نسبة الانتصارات الكبيرة.'),
      ('loss_margin_2plus_pct','خسارة بفارق هدفين أو أكثر',fmt_pct(pct(lambda r:r['gd']<=-2)),'نسبة الهزائم الكبيرة.'),
      ('under15_pct','أقل من 1.5 هدف بالمباراة',fmt_pct(pct(lambda r:r['total']<=1)),'نسبة المباريات التي انتهت بصفر أو هدف واحد.'),
      ('goals_2_3_pct','إجمالي 2 إلى 3 أهداف',fmt_pct(pct(lambda r:2<=r['total']<=3)),'نسبة المباريات التي شهدت هدفين أو ثلاثة.'),
      ('goals_4plus_pct','إجمالي 4 أهداف أو أكثر',fmt_pct(pct(lambda r:r['total']>=4)),'نسبة المباريات المفتوحة تهديفياً.'),
      ('first_half_scoring_pct','سجل في الشوط الأول',fmt_pct(pct(lambda r:r['ht_gf']>0,ht_rows)),'محسوب فقط للمباريات التي توفرت لها بيانات الشوط الأول.'),
      ('second_half_scoring_pct','سجل في الشوط الثاني',fmt_pct(pct(lambda r:(r['gf']-r['ht_gf'])>0,ht_rows)),'محسوب فقط للمباريات التي توفرت لها بيانات الشوط الأول.'),
      ('scored_both_halves_pct','سجل في الشوطين',fmt_pct(pct(lambda r:r['ht_gf']>0 and (r['gf']-r['ht_gf'])>0,ht_rows)),'نسبة مباريات العينة ذات بيانات الشوطين التي سجل فيها في كليهما.'),
      ('conceded_both_halves_pct','استقبل في الشوطين',fmt_pct(pct(lambda r:r['ht_ga']>0 and (r['ga']-r['ht_ga'])>0,ht_rows)),'نسبة مباريات العينة ذات بيانات الشوطين التي استقبل فيها في كليهما.'),
      ('ht_lead_pct','كان متقدماً عند الاستراحة',fmt_pct(pct(lambda r:r['ht_gf']>r['ht_ga'],ht_rows)),'يعتمد على نتائج الشوط الأول المتاحة فقط.'),
      ('ht_draw_pct','تعادل عند الاستراحة',fmt_pct(pct(lambda r:r['ht_gf']==r['ht_ga'],ht_rows)),'يعتمد على نتائج الشوط الأول المتاحة فقط.'),
      ('ht_trail_pct','كان متأخراً عند الاستراحة',fmt_pct(pct(lambda r:r['ht_gf']<r['ht_ga'],ht_rows)),'يعتمد على نتائج الشوط الأول المتاحة فقط.'),
      ('last3_points','نقاط آخر 3 مباريات',fmt(sum(r['points'] for r in last3)),'مجموع النقاط في أحدث ثلاث مباريات متاحة.'),
      ('last3_goal_difference','فارق أهداف آخر 3 مباريات',fmt(sum(r['gd'] for r in last3)),'فارق الأهداف في أحدث ثلاث مباريات.'),
      ('last3_avg_scored','متوسط التسجيل في آخر 3',fmt(mean([r['gf'] for r in last3])),'متوسط أهداف الفريق في أحدث ثلاث مباريات.'),
      ('last3_avg_conceded','متوسط الاستقبال في آخر 3',fmt(mean([r['ga'] for r in last3])),'متوسط الأهداف المستقبلة في أحدث ثلاث مباريات.'),
      ('recent_points_change','تغير النقاط: آخر 3 مقابل السابق',fmt(delta),'فرق متوسط النقاط لكل مباراة بين آخر 3 مباريات والسابق منها؛ موجب يعني تحسناً في العينة.'),
    ]
    return [{'key':k,'label':label,'value':value,'description':description,'sample':len(ht_rows) if k in {'first_half_scoring_pct','second_half_scoring_pct','scored_both_halves_pct','conceded_both_halves_pct','ht_lead_pct','ht_draw_pct','ht_trail_pct'} else n} for k,label,value,description in metrics]

def team_extras(team_id, fixtures):
    corners=[]; yellow=[]; red=[]; xg=[]; notes=[]
    for fx in fixtures:
        for block in fx.get('_statistics', []):
            if (block.get('team') or {}).get('id') != int(team_id): continue
            for stat in block.get('statistics') or []:
                name = str(stat.get('displayName') or '').strip().lower()
                value = stat.get('value')
                try: value = float(value)
                except (ValueError, TypeError): continue
                if 'corner' in name: corners.append(value)
                elif 'yellow' in name: yellow.append(value)
                elif 'red' in name: red.append(value)
                elif name in ('xg', 'expected goals', 'expected goals (xg)') or 'expected goals' in name: xg.append(value)
    if not any(fx.get('_statistics') for fx in fixtures):
        notes.append('إحصائيات المباريات التفصيلية غير متاحة لبعض أو كل المباريات حسب تغطية الدوري.')
    notes.append('قائمة الإصابات وبيانات odds ليست ضمن البيانات التي جرى ربطها في هذه النسخة.')
    return {'avg_corners':safe_mean(corners),'avg_yellow':safe_mean(yellow),'avg_red':safe_mean(red),'avg_xg':safe_mean(xg),
            'corners_sample':len(corners),'yellow_sample':len(yellow),'red_sample':len(red),'xg_sample':len(xg),
            'injuries':[],'extras_notes':notes,'stats_sample':max(len(corners),len(yellow),len(red),len(xg))}


def poisson(lam, k):
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def poisson_tail(lam, minimum):
    """P(X >= minimum) for a Poisson variable; used only as a transparent approximation."""
    if lam is None or minimum <= 0:
        return 1.0
    lam = max(0.0, float(lam))
    cdf = sum(poisson(lam, k) for k in range(minimum))
    return max(0.0, min(1.0, 1.0 - cdf))


def build_200_markets(matrix, home_lam, away_lam, a, b):
    """Build 200 distinct markets with only eight exact-score selections.

    Goal-derived markets use the same transparent Poisson score matrix. Corners/cards
    are estimated only from provider samples. Other event/player props remain unavailable
    until the corresponding event/player data are connected.
    """
    rows = []
    def add(group, label, probability, basis, sample=None, note=''):
        prob = None if probability is None else round(max(0.0, min(1.0, float(probability)))*100, 1)
        rows.append({
            'group': group, 'label': label, 'probability': prob,
            'fair_odds': round(100/prob, 2) if prob is not None and prob > 0 else None,
            'sample': sample, 'basis': basis,
            'note': note or ('تقدير نموذجي وليس سعراً من شركة مراهنات.' if prob is not None else 'غير متاح: لا توجد بيانات موثوقة كافية لهذا السوق.')
        })
    def mp(test):
        return sum(p for i,j,p in matrix if test(i,j))
    def add_ou(group, label, lam, thresholds, basis, sample=10, unavailable_note=''):
        for line in thresholds:
            over = poisson_tail(lam, int(line)+1) if lam is not None else None
            add(group, f'{label} أكثر من {line + .5:.1f}', over, basis, sample if over is not None else None, unavailable_note)
            add(group, f'{label} أقل من {line + .5:.1f}', 1-over if over is not None else None, basis, sample if over is not None else None, unavailable_note)

    home_win=mp(lambda i,j:i>j); draw=mp(lambda i,j:i==j); away_win=mp(lambda i,j:i<j)
    btts=mp(lambda i,j:i>0 and j>0)
    # 1-6: match result markets
    for label,prob in [('فوز صاحب الأرض',home_win),('تعادل',draw),('فوز الضيف',away_win),('فرصة مزدوجة 1X',home_win+draw),('فرصة مزدوجة X2',draw+away_win),('فوز أحد الفريقين 12',home_win+away_win)]:
        add('النتيجة النهائية',label,prob,'مصفوفة نتائج Poisson',len(matrix))
    # 7-20: match total goals O/U 0.5-6.5
    for line in range(7):
        over=mp(lambda i,j,n=line+1:i+j>=n)
        add('مجموع أهداف المباراة',f'أكثر من {line+.5:.1f}',over,'مصفوفة نتائج Poisson',len(matrix))
        add('مجموع أهداف المباراة',f'أقل من {line+.5:.1f}',1-over,'مصفوفة نتائج Poisson',len(matrix))
    # 21-22: BTTS
    add('تسجيل الفريقين','نعم — BTTS',btts,'مصفوفة نتائج Poisson',len(matrix))
    add('تسجيل الفريقين','لا — BTTS',1-btts,'مصفوفة نتائج Poisson',len(matrix))
    # 23-34: team goals O/U 0.5, 1.5, 2.5
    for label,lam in [('صاحب الأرض',home_lam),('الضيف',away_lam)]:
        add_ou('أهداف كل فريق',label,lam,(0,1,2),'توزيع Poisson لأهداف الفريق')
    # 35-42: exactly eight most likely scorelines, not a 121-score dump
    top_scores=sorted(matrix,key=lambda x:x[2],reverse=True)[:8]
    for i,j,p in top_scores:
        add('أفضل 8 نتائج دقيقة',f'{i}-{j}',p,'أعلى ثماني نتائج في مصفوفة Poisson',len(matrix),
            'هذه نتيجة دقيقة واحدة من ثماني نتائج معروضة؛ احتمالها منفرداً ولا يعني أنها مرجحة بقوة.')
    # 43-60: result + total-goal combinations (18)
    for outcome, predicate in [('فوز صاحب الأرض',lambda i,j:i>j),('تعادل',lambda i,j:i==j),('فوز الضيف',lambda i,j:i<j)]:
        for line in (1,2,3):
            add('نتيجة المباراة ومجموع الأهداف',f'{outcome} + أكثر من {line+.5:.1f}',mp(lambda i,j,p=predicate,n=line+1:p(i,j) and i+j>=n),'مصفوفة نتائج Poisson',len(matrix))
            add('نتيجة المباراة ومجموع الأهداف',f'{outcome} + أقل من {line+.5:.1f}',mp(lambda i,j,p=predicate,n=line+1:p(i,j) and i+j<n),'مصفوفة نتائج Poisson',len(matrix))
    # 61-68: BTTS + total goals combinations
    for line in (1,2,3,4):
        add('تسجيل الفريقين ومجموع الأهداف',f'BTTS نعم + أكثر من {line+.5:.1f}',mp(lambda i,j,n=line+1:i>0 and j>0 and i+j>=n),'مصفوفة نتائج Poisson',len(matrix))
        add('تسجيل الفريقين ومجموع الأهداف',f'BTTS لا + أقل من {line+.5:.1f}',mp(lambda i,j,n=line+1:not(i>0 and j>0) and i+j<n),'مصفوفة نتائج Poisson',len(matrix))
    # 69-73: clean sheets and win-to-nil
    add('الشباك النظيفة','ريال/صاحب الأرض يفوز دون استقبال',mp(lambda i,j:i>j and j==0),'مصفوفة نتائج Poisson',len(matrix))
    add('الشباك النظيفة','الضيف يفوز دون استقبال',mp(lambda i,j:j>i and i==0),'مصفوفة نتائج Poisson',len(matrix))
    add('الشباك النظيفة','صاحب الأرض يحافظ على نظافة شباكه',mp(lambda i,j:j==0),'مصفوفة نتائج Poisson',len(matrix))
    add('الشباك النظيفة','الضيف يحافظ على نظافة شباكه',mp(lambda i,j:i==0),'مصفوفة نتائج Poisson',len(matrix))
    add('الشباك النظيفة','كلا الفريقين لا يحافظان على شباك نظيفة',mp(lambda i,j:i>0 and j>0),'مصفوفة نتائج Poisson',len(matrix))
    # 74-79: winning margin
    for label,test in [('صاحب الأرض بفارق هدف',lambda i,j:i-j==1),('صاحب الأرض بفارق هدفين',lambda i,j:i-j==2),('صاحب الأرض بفارق 3+ أهداف',lambda i,j:i-j>=3),('الضيف بفارق هدف',lambda i,j:j-i==1),('الضيف بفارق هدفين',lambda i,j:j-i==2),('الضيف بفارق 3+ أهداف',lambda i,j:j-i>=3)]:
        add('هامش الفوز',label,mp(test),'مصفوفة نتائج Poisson',len(matrix))
    # 80-85: handicap markets
    for handicap in (0.5,1.5,2.5):
        add('الهانديكاب الآسيوي',f'صاحب الأرض -{handicap:.1f}',mp(lambda i,j,h=handicap:i-j>h),'مصفوفة نتائج Poisson',len(matrix))
        add('الهانديكاب الآسيوي',f'الضيف +{handicap:.1f}',mp(lambda i,j,h=handicap:i-j< h),'مصفوفة نتائج Poisson',len(matrix))
    # 86-89: total-goal bands
    for label,test in [('0 أو 1 هدف',lambda i,j:i+j<=1),('2 أو 3 أهداف',lambda i,j:2<=i+j<=3),('4 أو 5 أهداف',lambda i,j:4<=i+j<=5),('6 أهداف أو أكثر',lambda i,j:i+j>=6)]:
        add('نطاق مجموع الأهداف',label,mp(test),'مصفوفة نتائج Poisson',len(matrix))
    # 90-93: parity and DNB
    add('أنماط الأهداف','مجموع أهداف فردي',mp(lambda i,j:(i+j)%2==1),'مصفوفة نتائج Poisson',len(matrix))
    add('أنماط الأهداف','مجموع أهداف زوجي',mp(lambda i,j:(i+j)%2==0),'مصفوفة نتائج Poisson',len(matrix))
    non_draw=home_win+away_win
    add('بدون تعادل','صاحب الأرض يفوز (DNB)',home_win/non_draw if non_draw else None,'احتمال مشروط بعد استبعاد التعادل',len(matrix))
    add('بدون تعادل','الضيف يفوز (DNB)',away_win/non_draw if non_draw else None,'احتمال مشروط بعد استبعاد التعادل',len(matrix))

    # 94-113: corners (10 lines x over/under); only if both teams have provider samples
    corner_ok=(a.get('avg_corners') is not None and b.get('avg_corners') is not None and (a.get('corners_sample') or 0)>=1 and (b.get('corners_sample') or 0)>=1)
    corner_lam=(a.get('avg_corners') or 0)+(b.get('avg_corners') or 0) if corner_ok else None
    add_ou('الركنيات','إجمالي الركنيات',corner_lam,tuple(range(5,15)),'Poisson تقريبي من متوسط الركنيات المسترجع',
           (a.get('corners_sample',0)+b.get('corners_sample',0)) if corner_ok else None,
           '' if corner_ok else 'يتطلب عينات ركنيات تاريخية من تفاصيل المباريات؛ لا يتم تخمينها.')
    # 114-129: cards (3 total-yellow lines x2, red yes/no, 2 team lines x2)
    cards_ok=(a.get('avg_yellow') is not None and b.get('avg_yellow') is not None and (a.get('yellow_sample') or 0)>=1 and (b.get('yellow_sample') or 0)>=1)
    cards_lam=(a.get('avg_yellow') or 0)+(b.get('avg_yellow') or 0) if cards_ok else None
    add_ou('البطاقات الصفراء','إجمالي البطاقات الصفراء',cards_lam,(3,4,5),'Poisson تقريبي من متوسط البطاقات المسترجع',
           (a.get('yellow_sample',0)+b.get('yellow_sample',0)) if cards_ok else None,
           '' if cards_ok else 'يتطلب عينات بطاقات صفراء تاريخية من تفاصيل المباريات.')
    red_ok=(a.get('avg_red') is not None and b.get('avg_red') is not None and (a.get('red_sample') or 0)>=1 and (b.get('red_sample') or 0)>=1)
    red_lam=(a.get('avg_red') or 0)+(b.get('avg_red') or 0) if red_ok else None
    red_yes=(1-math.exp(-red_lam)) if red_lam is not None else None
    add('البطاقات الحمراء','ظهور بطاقة حمراء — نعم',red_yes,'Poisson من متوسط البطاقات الحمراء',
        (a.get('red_sample',0)+b.get('red_sample',0)) if red_ok else None,
        '' if red_ok else 'يتطلب عينات بطاقات حمراء تاريخية.')
    add('البطاقات الحمراء','عدم ظهور بطاقة حمراء — لا',1-red_yes if red_yes is not None else None,'Poisson من متوسط البطاقات الحمراء',
        (a.get('red_sample',0)+b.get('red_sample',0)) if red_ok else None,
        '' if red_ok else 'يتطلب عينات بطاقات حمراء تاريخية.')
    for team_label,team in [('صاحب الأرض',a),('الضيف',b)]:
        ok=team.get('avg_yellow') is not None and (team.get('yellow_sample') or 0)>=1
        add_ou('بطاقات كل فريق',f'بطاقات {team_label}',team.get('avg_yellow') if ok else None,(1,2),
               'Poisson تقريبي من متوسط بطاقات الفريق',team.get('yellow_sample') if ok else None,
               '' if ok else 'يتطلب إحصائيات بطاقات هذا الفريق.')
    # 130-147: half-time markets; unavailable unless actual HT event samples exist
    ht_ok=(a.get('ht_sample',0)>=1 and b.get('ht_sample',0)>=1 and all(x is not None for x in (a.get('ht_avg_gf'),a.get('ht_avg_ga'),b.get('ht_avg_gf'),b.get('ht_avg_ga'))))
    if ht_ok:
        hhl=max(.05,(a['ht_avg_gf']+b['ht_avg_ga'])/2*1.08); hal=max(.05,(b['ht_avg_gf']+a['ht_avg_ga'])/2)
        hm=[(i,j,poisson(hhl,i)*poisson(hal,j)) for i in range(9) for j in range(9)]; mass=sum(x[2] for x in hm) or 1
        hm=[(i,j,p/mass) for i,j,p in hm]
        hmp=lambda test:sum(p for i,j,p in hm if test(i,j))
        ht_home=hmp(lambda i,j:i>j); ht_draw=hmp(lambda i,j:i==j); ht_away=hmp(lambda i,j:i<j)
        ht_over=[hmp(lambda i,j,n=n:i+j>=n) for n in range(1,5)]
        ht_items=[('فوز صاحب الأرض في الشوط الأول',ht_home),('تعادل الشوط الأول',ht_draw),('فوز الضيف في الشوط الأول',ht_away),('أكثر من 0.5 هدف في الشوط الأول',ht_over[0]),('أقل من 0.5 هدف في الشوط الأول',1-ht_over[0]),('أكثر من 1.5 هدف في الشوط الأول',ht_over[1]),('أقل من 1.5 هدف في الشوط الأول',1-ht_over[1]),('أكثر من 2.5 هدف في الشوط الأول',ht_over[2]),('أقل من 2.5 هدف في الشوط الأول',1-ht_over[2]),('أكثر من 3.5 هدف في الشوط الأول',ht_over[3]),('أقل من 3.5 هدف في الشوط الأول',1-ht_over[3]),('BTTS الشوط الأول — نعم',hmp(lambda i,j:i>0 and j>0)),('BTTS الشوط الأول — لا',hmp(lambda i,j:i==0 or j==0)),('صاحب الأرض يسجل في الشوط الأول',hmp(lambda i,j:i>0)),('الضيف يسجل في الشوط الأول',hmp(lambda i,j:j>0)),('صاحب الأرض لا يسجل في الشوط الأول',hmp(lambda i,j:i==0)),('الضيف لا يسجل في الشوط الأول',hmp(lambda i,j:j==0)),('لا أهداف في الشوط الأول',hmp(lambda i,j:i==0 and j==0))]
    else:
        ht_note='غير متاح: يحتاج أحداث/إحصائيات الشوط الأول الفعلية للمباريات السابقة.'
        ht_items=[(x,None) for x in ['فوز صاحب الأرض في الشوط الأول','تعادل الشوط الأول','فوز الضيف في الشوط الأول','أكثر من 0.5 هدف في الشوط الأول','أقل من 0.5 هدف في الشوط الأول','أكثر من 1.5 هدف في الشوط الأول','أقل من 1.5 هدف في الشوط الأول','أكثر من 2.5 هدف في الشوط الأول','أقل من 2.5 هدف في الشوط الأول','أكثر من 3.5 هدف في الشوط الأول','أقل من 3.5 هدف في الشوط الأول','BTTS الشوط الأول — نعم','BTTS الشوط الأول — لا','صاحب الأرض يسجل في الشوط الأول','الضيف يسجل في الشوط الأول','صاحب الأرض لا يسجل في الشوط الأول','الضيف لا يسجل في الشوط الأول','لا أهداف في الشوط الأول']]
    for label,prob in ht_items:
        add('أسواق الشوط الأول',label,prob,'توزيع أهداف الشوط الأول' if ht_ok else 'بيانات الشوط الأول غير متاحة',a.get('ht_sample',0)+b.get('ht_sample',0) if ht_ok else None,'' if ht_ok else ht_note)
    # 148-167: match stats needing their own event-level historical samples
    unavailable_stats=[('إجمالي التسديدات','أكثر من 19.5'),('إجمالي التسديدات','أقل من 19.5'),('إجمالي التسديدات','أكثر من 21.5'),('إجمالي التسديدات','أقل من 21.5'),('إجمالي التسديدات','أكثر من 23.5'),('إجمالي التسديدات','أقل من 23.5'),('إجمالي التسديدات','أكثر من 25.5'),('إجمالي التسديدات','أقل من 25.5'),('التسديدات على المرمى','أكثر من 7.5'),('التسديدات على المرمى','أقل من 7.5'),('التسديدات على المرمى','أكثر من 8.5'),('التسديدات على المرمى','أقل من 8.5'),('التسديدات على المرمى','أكثر من 9.5'),('التسديدات على المرمى','أقل من 9.5'),('حالات التسلل','أكثر من 3.5'),('حالات التسلل','أقل من 3.5'),('حالات التسلل','أكثر من 4.5'),('حالات التسلل','أقل من 4.5'),('ركلة جزاء في المباراة','نعم'),('ركلة جزاء في المباراة','لا')]
    stats_note='غير متاح: يحتاج إحصائيات تاريخية مستقلة لهذا الحدث لكل مباراة؛ لم يتم اشتقاقه من الأهداف.'
    for group,label in unavailable_stats:
        add(group,label,None,'بيانات أحداث/إحصائيات غير مربوطة',None,stats_note)
    # 168-200: player-specific props. No player probabilities without lineups and per-player history.
    player_labels=['لاعب محدد يسجل في أي وقت','أول مسجل للهدف','آخر مسجل للهدف','لاعب يسجل هدفين أو أكثر','هاتريك لاعب محدد','لاعب يصنع هدفاً','لاعب يحصل على بطاقة','لاعب يسدد على المرمى مرة على الأقل','لاعب يسدد على المرمى مرتين أو أكثر','لاعب يسدد 3 مرات أو أكثر','لاعب يصنع فرصتين أو أكثر','لاعب يصنع 3 فرص أو أكثر','لاعب يرتكب خطأين أو أكثر','لاعب يتعرض لخطأين أو أكثر','حارس مرمى يحافظ على شباك نظيفة','حارس مرمى يتصدى لـ 3 تسديدات أو أكثر','حارس مرمى يتصدى لـ 5 تسديدات أو أكثر','مدافع يسجل هدفاً','مدافع يحصل على بطاقة','مهاجم يسجل هدفاً','لاعب بديل يسجل','لاعب يسجل في الشوط الأول','لاعب يسجل في الشوط الثاني','لاعب يسجل بالرأس','لاعب يسجل من ركلة جزاء','لاعب يسجل من خارج المنطقة','لاعب يقدم تمريرة حاسمة أولى','لاعب يقدم تمريرتين حاسمتين','لاعب يسجل ويصنع في المباراة','لاعب يسجل أكثر من هدف','لاعب ينفذ ركلة جزاء','لاعب ينفذ 3+ تسديدات','لاعب يحصل على طرد']
    player_note='غير متاح: يحتاج تشكيلة مؤكدة/متوقعة وبيانات اللاعب بالدقائق والأهداف والتسديدات والتمريرات؛ لا يتم اختلاق نسبة للاعب غير محدد.'
    for label in player_labels:
        add('أسواق اللاعبين',label,None,'بيانات اللاعبين والتشكيلات غير مربوطة',None,player_note)

    # Keep the contract strict: 200 rows, exactly 8 scoreline markets.
    if len(rows) != 200:
        raise ValueError(f'Expected 200 markets, got {len(rows)}')
    if sum(1 for row in rows if row['group']=='أفضل 8 نتائج دقيقة') != 8:
        raise ValueError('Expected exactly eight exact-score markets')
    return rows


def model_probs(a, b):
    if a['avg_gf'] is None or a['avg_ga'] is None or b['avg_gf'] is None or b['avg_ga'] is None or min(a['played'],b['played']) < 5:
        return None
    # Transparent Poisson estimate. Blend overall form with venue-specific rates only
    # when both venue samples exist; shrink the venue component heavily for tiny samples.
    home_base = (a['avg_gf'] + b['avg_ga'])/2 * 1.08
    away_base = (b['avg_gf'] + a['avg_ga'])/2
    home_lam, away_lam = home_base, away_base
    venue_n = min(int(a.get('home_played') or 0), int(b.get('away_played') or 0))
    venue_weight = min(0.30, 0.30 * venue_n / 3.0)
    if venue_n > 0 and all(a.get(k) is not None for k in ('home_avg_gf','home_avg_ga')) and all(b.get(k) is not None for k in ('away_avg_gf','away_avg_ga')):
        venue_home = (a['home_avg_gf'] + b['away_avg_ga'])/2 * 1.08
        venue_away = (b['away_avg_gf'] + a['home_avg_ga'])/2
        home_lam = (1-venue_weight)*home_base + venue_weight*venue_home
        away_lam = (1-venue_weight)*away_base + venue_weight*venue_away
    # Small, capped form-momentum adjustment: last three versus the previous two.
    def points_for(result): return 3 if result == 'W' else (1 if result == 'D' else 0)
    def recent_momentum(team):
        form = team.get('form') or []
        recent, previous = form[:3], form[3:5]
        if len(recent) < 2 or not previous: return 0.0
        return (sum(points_for(x) for x in recent)/len(recent)) - (sum(points_for(x) for x in previous)/len(previous))
    home_momentum = max(-1.5, min(1.5, recent_momentum(a)))
    away_momentum = max(-1.5, min(1.5, recent_momentum(b)))
    home_lam *= math.exp(0.025 * home_momentum)
    away_lam *= math.exp(0.025 * away_momentum)
    home_lam = max(.15, home_lam)
    away_lam = max(.15, away_lam)
    limit = 11
    ph=[poisson(home_lam,i) for i in range(limit)]
    pa=[poisson(away_lam,i) for i in range(limit)]
    matrix=[]
    for i,p1 in enumerate(ph):
        for j,p2 in enumerate(pa):
            matrix.append((i,j,p1*p2))
    mass=sum(p for _,_,p in matrix) or 1
    matrix=[(i,j,p/mass) for i,j,p in matrix]
    win=draw=loss=0.0
    totals={k:0.0 for k in range(0,6)}
    btts=0.0; home_scores={k:0.0 for k in range(0,4)}; away_scores={k:0.0 for k in range(0,4)}
    for i,j,p in matrix:
        if i>j: win+=p
        elif i==j: draw+=p
        else: loss+=p
        total=i+j
        for threshold in totals:
            if total >= threshold: totals[threshold]+=p
        if i>0 and j>0: btts+=p
        for k in home_scores:
            if i>=k: home_scores[k]+=p
            if j>=k: away_scores[k]+=p
    def pc(v): return round(v*100,1)
    markets={
        'home_win':pc(win),'draw':pc(draw),'away_win':pc(loss),
        'double_1x':pc(win+draw),'double_x2':pc(draw+loss),'double_12':pc(win+loss),
        'btts_yes':pc(btts),'btts_no':pc(1-btts),
        'over05':pc(totals[1]),'under05':pc(1-totals[1]),
        'over15':pc(totals[2]),'under15':pc(1-totals[2]),
        'over25':pc(totals[3]),'under25':pc(1-totals[3]),
        'over35':pc(totals[4]),'under35':pc(1-totals[4]),
        'over45':pc(totals[5]),'under45':pc(1-totals[5]),
        'home_over05':pc(home_scores[1]),'home_under05':pc(1-home_scores[1]),'home_over15':pc(home_scores[2]),'home_under15':pc(1-home_scores[2]),'home_over25':pc(home_scores[3]),'home_under25':pc(1-home_scores[3]),
        'away_over05':pc(away_scores[1]),'away_under05':pc(1-away_scores[1]),'away_over15':pc(away_scores[2]),'away_under15':pc(1-away_scores[2]),'away_over25':pc(away_scores[3]),'away_under25':pc(1-away_scores[3]),
    }
    top_scores=sorted(matrix,key=lambda x:x[2],reverse=True)[:8]
    top_scores=[{'score':f'{i}-{j}','probability':pc(p)} for i,j,p in top_scores]
    # Compare a small set of common markets by probability only; actual value requires bookmaker odds.
    candidates=[('فرصة مزدوجة 1X',markets['double_1x'],'اتحاد فوز صاحب الأرض أو التعادل.'),
                ('فرصة مزدوجة X2',markets['double_x2'],'اتحاد التعادل أو فوز الفريق الضيف.'),
                ('أكثر من 1.5 هدف',markets['over15'],'النموذج يتوقع هدفين أو أكثر في المباراة.'),
                ('أقل من 4.5 أهداف',markets['under45'],'النموذج يضع احتمالاً لعدم تجاوز أربعة أهداف.'),
                ('تسجيل الفريقين BTTS',markets['btts_yes'],'كلا الفريقين يسجلان هدفاً واحداً على الأقل.'),
                ('عدم تسجيل الفريقين BTTS No',markets['btts_no'],'أحد الفريقين على الأقل لا يسجل.')]
    pick=max(candidates,key=lambda x:x[1])
    pick_obj={'name':pick[0],'probability':pick[1],'fair_odds':round(100/pick[1],2) if pick[1]>0 else None,'reason':pick[2]}
    return {'home_lambda':round(home_lam,2),'away_lambda':round(away_lam,2),
            'expected_score':top_scores[0]['score'],
            'expected_goals':{'home':round(home_lam,2),'away':round(away_lam,2)},
            'home_win':pc(win),'draw':pc(draw),'away_win':pc(loss),
            'over25':pc(totals[3]),'over35':pc(totals[4]),'btts':pc(btts),
            'markets':markets,'markets_200':build_200_markets(matrix,home_lam,away_lam,a,b),'top_scores':top_scores,'statistical_pick':pick_obj,'venue_sample_weight':round(venue_weight,3),'momentum_adjustment':{'home':round(home_momentum,2),'away':round(away_momentum,2)},
            'model':'نموذج Poisson تجريبي: يمزج متوسط آخر خمس مباريات مع أداء الأرض/الخارج عند توفر عينة، ويطبق تعديلاً محدوداً لاتجاه النقاط الحديث. ليس xG ولا نموذجاً معايراً أو ضماناً.'}


@app.route('/')
def home():
    return render_template('index.html')


@app.get('/api/status')
def status():
    return jsonify({'api_configured':bool(API_KEY),'provider':'Highlightly','mode':'live' if API_KEY else 'manual',
                    'server_daily_calls':_DAILY_API_CALLS,'server_daily_soft_limit':_DAILY_API_SOFT_LIMIT,
                    'provider_remaining':_LAST_REMAINING,'provider_quota_exhausted':_PROVIDER_QUOTA_EXHAUSTED,
                    'detailed_match_stats_enabled':_ENABLE_DETAILED_MATCH_STATS})


LIVE_STATES = {'first half', 'second half', 'half time', 'extra time', 'break time', 'penalties', 'in progress', 'suspended', 'interrupted'}
FINISHED_STATES = {'finished', 'finished after penalties', 'finished after extra time', 'cancelled', 'abandoned', 'awarded'}


def unwrap_match(payload):
    if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
        return payload['data']
    return first_record(payload)


def match_state_payload(item):
    state = item.get('state') or {}
    home = item.get('homeTeam') or {}
    away = item.get('awayTeam') or {}
    score = state.get('score') or {}
    return {
        'id': item.get('id'),
        'date': item.get('date'),
        'round': item.get('round') or '',
        'league': (item.get('league') or {}).get('name') or '',
        'status': state.get('description') or 'Unknown',
        'clock': state.get('clock'),
        'score': score.get('current') or '',
        'home': {'id': home.get('id'), 'name': home.get('name') or 'صاحب الأرض', 'logo': home.get('logo') or ''},
        'away': {'id': away.get('id'), 'name': away.get('name') or 'الضيف', 'logo': away.get('logo') or ''},
    }


@app.get('/api/match-center')
def match_center_find():
    """Find the scheduled fixture for the exact selected home/away pair."""
    try:
        home_id = int(request.args['home_id'])
        away_id = int(request.args['away_id'])
        if home_id == away_id:
            return jsonify({'error': 'اختر فريقين مختلفين.'}), 400
        payload = api_get('/matches', {
            'homeTeamId': home_id,
            'awayTeamId': away_id,
            'timezone': 'Africa/Algiers',
            'limit': 20,
            'offset': 0,
        }, cache_seconds=900)
        fixtures = payload_data(payload)
        fixtures = [x for x in fixtures if isinstance(x, dict) and x.get('id') is not None]
        now = datetime.now(timezone.utc)
        def kickoff(item):
            try:
                value = str(item.get('date') or '').replace('Z', '+00:00')
                dt = datetime.fromisoformat(value)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        live = [x for x in fixtures if str((x.get('state') or {}).get('description', '')).lower() in LIVE_STATES]
        upcoming = [x for x in fixtures if kickoff(x) >= now and str((x.get('state') or {}).get('description', '')).lower() not in FINISHED_STATES]
        if live:
            chosen = sorted(live, key=kickoff)[0]
        elif upcoming:
            chosen = sorted(upcoming, key=kickoff)[0]
        else:
            return jsonify({'found': False, 'message': 'لم يعثر المزود على مواجهة قادمة بهذا الترتيب بين الفريقين. قد لا تكون المباراة مجدولة أو لا تغطيها قاعدة البيانات حالياً.'})
        return jsonify({'found': True, 'match': match_state_payload(chosen), 'updated_at': datetime.now(timezone.utc).isoformat(), 'poll_seconds': 300})
    except KeyError:
        return jsonify({'error': 'اختر الفريقين أولاً.'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.get('/api/match-center/<int:match_id>')
def match_center_update(match_id):
    """Fresh match state plus provider live events; cached briefly for shared viewers."""
    try:
        payload = api_get(f'/matches/{match_id}', cache_seconds=240)
        item = unwrap_match(payload)
        if not item or not item.get('id'):
            return jsonify({'error': 'لم تصل تفاصيل المباراة من المزود.'}), 502
        result = match_state_payload(item)
        events = item.get('events') if isinstance(item.get('events'), list) else []
        clean_events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            team = event.get('team') or {}
            clean_events.append({
                'time': event.get('time') or '',
                'type': event.get('type') or 'Event',
                'team': {'id': team.get('id'), 'name': team.get('name') or '', 'logo': team.get('logo') or ''},
                'player': event.get('player') or '',
                'assist': event.get('assist') or '',
                'substituted': event.get('substituted') or '',
            })
        result['events'] = clean_events
        result['events_available'] = isinstance(item.get('events'), list)
        result['updated_at'] = datetime.now(timezone.utc).isoformat()
        result['provider_clock_available'] = result.get('clock') is not None
        return jsonify(result)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


# Team catalogue is cached for 24 hours. Search is performed locally after the first API fetch,
# so typing several letters does not consume one Highlightly request per keystroke.
_TEAM_CATALOGUE = {'time': 0, 'items': []}
_TEAM_CATALOGUE_LOCK = threading.Lock()

# Common Arabic names, transliterations and nicknames mapped to the provider's canonical club name.
# The fuzzy transliteration below handles many additional Arabic spellings automatically.
TEAM_ALIASES = {
    'ريال مدريد': ['Real Madrid'], 'ريال': ['Real Madrid'], 'الملكي': ['Real Madrid'], 'ريالمدريد': ['Real Madrid'],
    'برشلونة': ['Barcelona'], 'بارسا': ['Barcelona'], 'البارسا': ['Barcelona'], 'برشا': ['Barcelona'],
    'مانشستر سيتي': ['Manchester City'], 'مان سيتي': ['Manchester City'], 'السيتي': ['Manchester City'],
    'مانشستر يونايتد': ['Manchester United'], 'مان يونايتد': ['Manchester United'], 'اليونايتد': ['Manchester United'],
    'ليفربول': ['Liverpool'], 'ليفر': ['Liverpool'], 'ارسنال': ['Arsenal'], 'أرسنال': ['Arsenal'],
    'تشيلسي': ['Chelsea'], 'توتنهام': ['Tottenham Hotspur'], 'نيوكاسل': ['Newcastle United'],
    'باريس سان جيرمان': ['Paris Saint-Germain'], 'باريس': ['Paris Saint-Germain'], 'بي اس جي': ['Paris Saint-Germain'], 'psg': ['Paris Saint-Germain'],
    'بايرن ميونخ': ['Bayern Munich'], 'بايرن': ['Bayern Munich'], 'دورتموند': ['Borussia Dortmund'],
    'يوفنتوس': ['Juventus'], 'اليوفي': ['Juventus'], 'انتر': ['Inter'], 'إنتر': ['Inter'], 'ميلان': ['AC Milan'],
    'اتلتيكو مدريد': ['Atletico Madrid'], 'أتلتيكو مدريد': ['Atletico Madrid'], 'اتليتيكو': ['Atletico Madrid'],
    'اشبيلية': ['Sevilla'], 'إشبيلية': ['Sevilla'], 'نابولي': ['Napoli'], 'روما': ['Roma'],
    'بنفيكا': ['Benfica'], 'بورتو': ['Porto'], 'اياكس': ['Ajax'], 'أياكس': ['Ajax'],
    'الهلال': ['Al Hilal'], 'النصر': ['Al Nassr'], 'الاتحاد': ['Al Ittihad'], 'الأهلي': ['Al Ahli'],
    'الاهلي': ['Al Ahli'], 'العين': ['Al Ain'], 'الوداد': ['Wydad'], 'الرجاء': ['Raja Casablanca'],
}

_ARABIC_TO_LATIN = str.maketrans({
    'ا':'a','أ':'a','إ':'i','آ':'a','ٱ':'a','ب':'b','ت':'t','ث':'th','ج':'j','ح':'h','خ':'kh',
    'د':'d','ذ':'dh','ر':'r','ز':'z','س':'s','ش':'sh','ص':'s','ض':'d','ط':'t','ظ':'z','ع':'a',
    'غ':'gh','ف':'f','ق':'q','ك':'k','ل':'l','م':'m','ن':'n','ه':'h','ة':'h','و':'w','ؤ':'w',
    'ي':'y','ى':'a','ئ':'y','ء':'','پ':'p','چ':'ch','ڤ':'v','گ':'g'
})

def normalize_team_text(value):
    value = unicodedata.normalize('NFKD', str(value or '').lower())
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = value.translate(_ARABIC_TO_LATIN)
    value = value.replace('&', ' and ')
    value = re.sub(r'[^a-z0-9]+', '', value)
    return value

def get_team_catalogue():
    now = time.time()
    with _TEAM_CATALOGUE_LOCK:
        if _TEAM_CATALOGUE['items'] and now - _TEAM_CATALOGUE['time'] < 24 * 60 * 60:
            return _TEAM_CATALOGUE['items']
    # Highlightly documents /teams as the team-id lookup endpoint; one cached catalogue
    # enables tolerant local matching instead of exact-name API queries on every keystroke.
    payload = api_get('/teams', {'limit': 500, 'offset': 0})
    items = [x for x in payload_data(payload) if isinstance(x, dict) and x.get('id') is not None and x.get('name')]
    pagination = payload.get('pagination', {}) if isinstance(payload, dict) else {}
    try:
        total = int(pagination.get('totalCount') or len(items))
    except (TypeError, ValueError):
        total = len(items)
    # Read a few pages once per day so clubs outside the first alphabetic page can be found.
    # The cap protects the user's free daily API quota.
    for offset in range(500, min(total, 2500), 500):
        page = api_get('/teams', {'limit': 500, 'offset': offset})
        items.extend(x for x in payload_data(page) if isinstance(x, dict) and x.get('id') is not None and x.get('name'))
    unique = {}
    for item in items:
        unique[str(item.get('id'))] = item
    items = list(unique.values())
    with _TEAM_CATALOGUE_LOCK:
        _TEAM_CATALOGUE['items'] = items
        _TEAM_CATALOGUE['time'] = now
    return items

def team_search_score(query, team):
    qn = normalize_team_text(query)
    name = str(team.get('name') or '')
    nn = normalize_team_text(name)
    if not qn or not nn: return 0.0
    score = SequenceMatcher(None, qn, nn).ratio()
    if nn.startswith(qn) or qn.startswith(nn): score = max(score, 0.96 if len(qn) >= 3 else 0.75)
    if qn in nn or nn in qn: score = max(score, 0.94 if len(qn) >= 3 else 0.72)
    # Token overlap helps searches such as "Madrid" or "United".
    q_tokens = re.findall(r'[a-z0-9]+', str(query).lower())
    n_tokens = re.findall(r'[a-z0-9]+', name.lower())
    if q_tokens and n_tokens and any(t in n_tokens for t in q_tokens): score = max(score, 0.88)
    # Compare the user's Arabic spelling with an approximate Latin transliteration.
    translit = normalize_team_text(query)
    if translit and SequenceMatcher(None, translit, nn).ratio() > score:
        score = SequenceMatcher(None, translit, nn).ratio()
    return score

@app.get('/api/search-team')
def search_team():
    q=request.args.get('q','').strip()
    if not q: return jsonify({'results':[]})
    try:
        teams = get_team_catalogue()
        aliases = []
        qn = normalize_team_text(q)
        for alias, targets in TEAM_ALIASES.items():
            an = normalize_team_text(alias)
            # Alias can match exact, partial or a small typo in the Arabic/Latin query.
            if an and (an == qn or an.startswith(qn) or qn.startswith(an) or SequenceMatcher(None, an, qn).ratio() >= 0.72):
                aliases.extend(targets)
        ranked=[]
        for team in teams:
            score=team_search_score(q, team)
            canonical=normalize_team_text(team.get('name'))
            if any(normalize_team_text(target) == canonical for target in aliases): score=max(score, 1.0)
            if score >= 0.34:
                ranked.append((score, team))
        ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get('name',''))))
        results=[]
        seen=set()
        for score, team in ranked[:15]:
            tid=str(team.get('id'))
            if tid in seen: continue
            seen.add(tid)
            results.append({'id':team.get('id'),'name':team.get('name'),'country':team.get('country') or '',
                            'logo':team.get('logo'),'match_score':round(score*100)})
        return jsonify({'results':results,'query':q,'search_mode':'fuzzy_arabic_latin','catalogue_count':len(teams)})
    except Exception as e:
        return jsonify({'error':str(e)}),400


@app.get('/api/analyze')
def analyze():
    try:
        a_id=int(request.args['home_id']); b_id=int(request.args['away_id'])
        if a_id == b_id: return jsonify({'error':'اختر فريقين مختلفين.'}),400
        a_raw=get_team_last_five(a_id); b_raw=get_team_last_five(b_id)
        a_fx=stats_for_fixtures(a_raw); b_fx=stats_for_fixtures(b_raw)
        a=team_metrics(a_id,a_fx); b=team_metrics(b_id,b_fx)
        a.update(team_extras(a_id,a_fx)); b.update(team_extras(b_id,b_fx))
        a['advanced_metrics'] = advanced_team_metrics(a_id, a_fx)
        b['advanced_metrics'] = advanced_team_metrics(b_id, b_fx)
        model=model_probs(a,b)
        warnings=[]
        if min(a['played'],b['played'])<5: warnings.append('لم تتوفر 5 مباريات مكتملة لكل فريق؛ نموذج الاحتمالات غير متاح أو محدود.')
        if not a['played'] or not b['played']: warnings.append('لا توجد نتائج مكتملة كافية لأحد الفريقين ضمن تغطية المزود.')
        if a['played'] < len([x for x in a_fx if x['home_goals'] is not None]) or b['played'] < len([x for x in b_fx if x['home_goals'] is not None]):
            warnings.append('تم استبعاد سجلات لا تطابق معرّف الفريق لتجنب احتساب فوز أو خسارة بشكل خاطئ.')
        if any(x['ht_home'] is None for x in a_fx+b_fx): warnings.append('إحصائيات الشوط الأول قد تكون ناقصة إذا لم يوفر المزود أحداث المباراة.')
        return jsonify({'team_a':a,'team_b':b,'model':model,'warnings':warnings,
                        'data_note':'المصدر Highlightly. تم جلب آخر خمس مباريات مكتملة فقط لكل فريق. يعرض التقرير 200 سوقاً مختلفة، منها 8 نتائج دقيقة فقط. أسواق الأهداف والنتيجة محسوبة من نموذج Poisson، أما الركنيات والبطاقات والشوط الأول فلا تظهر احتمالاتها إلا عند توفر تفاصيل تاريخية من المزود. أسواق اللاعبين غير متاحة دون بيانات لاعبين وتشكيلات. التفاصيل الموسعة متوقفة افتراضياً لتوفير حصة API المجانية. لا يتم تخمين البيانات الناقصة.',
                        'generated_at':datetime.now(timezone.utc).isoformat()})
    except KeyError:
        return jsonify({'error':'اختر الفريقين من نتائج البحث أولاً.'}),400
    except Exception as e:
        return jsonify({'error':str(e)}),400


if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=False)

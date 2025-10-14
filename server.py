from flask import Flask, request, jsonify, render_template, send_from_directory
import requests
import pandas as pd
import os
import re
from time import time
from functools import wraps
from flask_caching import Cache

app = Flask(__name__)

# 캐시 설정
cache = Cache(app, config={'CACHE_TYPE': 'simple'})

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/robots.txt')
def robots():
    return send_from_directory('static', 'robots.txt')

@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory('static', 'sitemap.xml')

@app.route('/index.html')
def index_html():
    return render_template('index.html')


# -----------------------------
# API 키 및 파일 경로
# -----------------------------
ALADIN_API_KEY = 'ttbj0124hkm1509002'
LIBRARY_API_KEY = 'c7a9b167110c242ad1634d7898b6970778f961fcefc0e6da9d52273b0ebb53ea'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIBRARY_LIST_FILE = os.path.join(BASE_DIR, 'library_list.xlsx')

library_data = None


# -----------------------------
# Helper: 주소 분리
# -----------------------------
def split_address(addr):
    if not isinstance(addr, str):
        return '', ''
    pattern = r'^(서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|경기도|강원도|충청북도|충청남도|전라북도|전라남도|경상북도|경상남도|제주특별자치도)\s+([^\s]+)'
    match = re.match(pattern, addr)
    return match.groups() if match else ('', '')


# -----------------------------
# 도서관 목록 로드
# -----------------------------
def load_library_data():
    global library_data
    if library_data is not None:
        return library_data

    if not os.path.exists(LIBRARY_LIST_FILE):
        print('library_list.xlsx 파일이 없습니다.')
        return []

    df = pd.read_excel(LIBRARY_LIST_FILE, usecols=['도서관명', '주소', '도서관코드'])

    def extract_region(addr):
        return addr.split()[0] if isinstance(addr, str) and len(addr.split()) > 0 else ''

    def extract_district(addr):
        return addr.split()[1] if isinstance(addr, str) and len(addr.split()) > 1 else ''

    library_data = []
    for _, row in df.iterrows():
        library_data.append({
            'libraryName': row['도서관명'],
            'address': row['주소'],
            'libraryCode': str(row['도서관코드']),
            'region': extract_region(row['주소']),
            'district': extract_district(row['주소'])
        })
    return library_data


# -----------------------------
# 도서관 분류 정보 반환
# -----------------------------
@app.route('/libraries')
def libraries():
    libs = load_library_data()

    regions = sorted(set(lib['region'] for lib in libs if lib['region']))

    districts_by_region = {}
    for region in regions:
        districts = sorted(set(lib['district'] for lib in libs if lib['region'] == region and lib['district']))
        districts_by_region[region] = districts

    libraries_by_district = {}
    for lib in libs:
        key = f"{lib['region']}|{lib['district']}"
        libraries_by_district.setdefault(key, []).append({
            'libraryName': lib['libraryName'],
            'libraryCode': lib['libraryCode']
        })

    return jsonify({
        'regions': regions,
        'districtsByRegion': districts_by_region,
        'librariesByDistrict': libraries_by_district
    })


# -----------------------------
# Rate Limiting (1분당 10회)
# -----------------------------
MAX_CALLS_PER_MINUTE = 10
call_records = {}

def rate_limit(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        client_ip = request.remote_addr or 'unknown'
        now = time()
        window_start = now - 60
        calls = [t for t in call_records.get(client_ip, []) if t > window_start]
        if len(calls) >= MAX_CALLS_PER_MINUTE:
            return jsonify({"error": "API 호출 한도를 초과했습니다. 잠시 후 다시 시도해주세요."}), 429
        calls.append(now)
        call_records[client_ip] = calls
        return func(*args, **kwargs)
    return wrapper


# -----------------------------
# 알라딘 도서 검색
# -----------------------------
@app.route('/search')
@rate_limit
def search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({'error': '검색어를 입력하세요.'}), 400

    cache_key = f"search:{query}"
    cached = cache.get(cache_key)
    if cached:
        return jsonify(cached)

    url = 'https://www.aladin.co.kr/ttb/api/ItemSearch.aspx'
    params = {
        'TTBKey': ALADIN_API_KEY,
        'Query': query,
        'QueryType': 'Keyword',
        'MaxResults': 48,
        'Start': 1,
        'SearchTarget': 'Book',
        'output': 'js',
        'Version': '20131101'
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        items = data.get('item', [])
        result_items = [{
            'title': b.get('title'),
            'author': b.get('author'),
            'publisher': b.get('publisher'),
            'pubDate': b.get('pubDate'),
            'cover': b.get('cover'),
            'priceStandard': b.get('priceStandard'),
            'priceSales': b.get('priceSales'),
            'link': b.get('link'),
            'isbn13': b.get('isbn13') or b.get('isbn'),
        } for b in items]

        cache.set(cache_key, {'item': result_items}, timeout=300)
        return jsonify({'item': result_items})
    except Exception as e:
        return jsonify({'error': f'알라딘 API 호출 실패: {e}'}), 500


# -----------------------------
# 도서관별 소장/대출 확인
# -----------------------------
@app.route('/check_library')
@rate_limit
def check_library():
    isbn = request.args.get('isbn', '').strip()
    library_code = request.args.get('libraryCode', '').strip()
    if not isbn or not library_code:
        return jsonify({'error': 'ISBN과 libraryCode를 모두 전달해야 합니다.'}), 400

    cache_key = f"check_library:{isbn}:{library_code}"
    cached = cache.get(cache_key)
    if cached:
        return jsonify(cached)

    libs = load_library_data()
    target = next((lib for lib in libs if lib['libraryCode'] == library_code), None)
    if not target:
        return jsonify({'error': '해당 도서관 코드가 없습니다.'}), 404

    url = 'http://data4library.kr/api/bookExist'
    params = {
        'authKey': LIBRARY_API_KEY,
        'libCode': target['libraryCode'],
        'isbn13': isbn,
        'format': 'json'
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        result = data.get('response', {}).get('result', {})
        has_book = result.get('hasBook', 'N') == 'Y'
        loan_available = result.get('loanAvailable', 'N') == 'Y'

        response_data = {
            'results': [{
                'libraryName': target['libraryName'],
                'hasBook': has_book,
                'loanAvailable': loan_available
            }]
        }
        cache.set(cache_key, response_data, timeout=300)
        return jsonify(response_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# -----------------------------
# ✅ 새 기능: 지역별 보유/대출현황 조회
# -----------------------------
@app.route('/check_region')
@rate_limit
def check_region():
    isbn = request.args.get('isbn', '').strip()
    region = request.args.get('region', '').strip()
    district = request.args.get('district', '').strip()

    if not isbn or not region or not district:
        return jsonify({'error': 'isbn, region, district가 필요합니다.'}), 400

    cache_key = f"check_region:{isbn}:{region}:{district}"
    cached = cache.get(cache_key)
    if cached:
        return jsonify(cached)

    libs = load_library_data()
    target_libs = [lib for lib in libs if lib['region'] == region and lib['district'] == district]

    if not target_libs:
        return jsonify({'results': []})

    results = []
    for lib in target_libs:
        try:
            url = 'http://data4library.kr/api/bookExist'
            params = {
                'authKey': LIBRARY_API_KEY,
                'libCode': lib['libraryCode'],
                'isbn13': isbn,
                'format': 'json'
            }
            resp = requests.get(url, params=params, timeout=4)
            data = resp.json()
            result = data.get('response', {}).get('result', {})
            has_book = result.get('hasBook', 'N') == 'Y'
            loan_available = result.get('loanAvailable', 'N') == 'Y'
            results.append({
                'libraryName': lib['libraryName'],
                'hasBook': has_book,
                'loanAvailable': loan_available
            })
        except Exception:
            results.append({
                'libraryName': lib['libraryName'],
                'hasBook': False,
                'loanAvailable': False
            })

    final_data = {'results': results}
    cache.set(cache_key, final_data, timeout=600)
    return jsonify(final_data)


# -----------------------------
# 실행
# -----------------------------
# if __name__ == '__main__':
#    port = int(os.environ.get("PORT", 10000))
#    app.run(host='0.0.0.0', port=port, debug=True)
    

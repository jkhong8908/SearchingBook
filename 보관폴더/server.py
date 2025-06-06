from flask import Flask, request, jsonify, render_template
import requests
import pandas as pd
import os
import re
from time import time
from functools import wraps
from flask_caching import Cache

app = Flask(__name__)

# 캐시 설정 (메모리 캐시)
cache = Cache(app, config={'CACHE_TYPE': 'simple'})

@app.route('/')
def home():
    return render_template('index.html')

# 환경변수 또는 config로 설정하세요.
ALADIN_API_KEY = 'ttbj0124hkm1509002'  # 알라딘 API 키
LIBRARY_API_KEY = 'c7a9b167110c242ad1634d7898b6970778f961fcefc0e6da9d52273b0ebb53ea'  # 공공도서관 API 키

# 도서관 목록 엑셀 파일 경로
LIBRARY_LIST_FILE = 'library_list.xlsx'

# 도서관 목록 캐시
library_data = None


def split_address(addr):
    """
    주소에서 시도(지역)와 시군구(구/군) 분리
    예: '서울특별시 강남구 역삼동 123-45' -> ('서울특별시', '강남구')
    """
    if not isinstance(addr, str):
        return '', ''

    pattern = r'^(서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|경기도|강원도|충청북도|충청남도|전라북도|전라남도|경상북도|경상남도|제주특별자치도)\s+([^\s]+)'
    match = re.match(pattern, addr)
    if match:
        return match.group(1), match.group(2)
    else:
        return '', ''


def load_library_data():  # 캐싱 목적
    global library_data
    if library_data is not None:
        return library_data

    if not os.path.exists(LIBRARY_LIST_FILE):
        print('library_list.xlsx 파일이 없습니다.')
        return []

    df = pd.read_excel(LIBRARY_LIST_FILE, usecols=['도서관명', '주소', '도서관코드'])

    def extract_region(addr):
        if not isinstance(addr, str):
            return ''
        parts = addr.split()
        if len(parts) > 0:
            return parts[0]
        return ''

    def extract_district(addr):
        if not isinstance(addr, str):
            return ''
        parts = addr.split()
        if len(parts) > 1:
            return parts[1]
        return ''

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
        if key not in libraries_by_district:
            libraries_by_district[key] = []
        libraries_by_district[key].append({
            'libraryName': lib['libraryName'],
            'libraryCode': lib['libraryCode'],
        })

    return jsonify({
        'regions': regions,
        'districtsByRegion': districts_by_region,
        'librariesByDistrict': libraries_by_district
    })


# --- Rate Limiting 기능 추가 ---

MAX_CALLS_PER_MINUTE = 10
call_records = {}  # {client_ip: [timestamp1, timestamp2, ...]}

def rate_limit(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        client_ip = request.remote_addr or 'unknown'
        now = time()
        window_start = now - 60  # 60초

        calls = call_records.get(client_ip, [])
        # 60초 이전 호출 기록 제거
        calls = [t for t in calls if t > window_start]

        if len(calls) >= MAX_CALLS_PER_MINUTE:
            return jsonify({"error": "API 호출 한도를 초과했습니다. 잠시 후 다시 시도해주세요."}), 429

        calls.append(now)
        call_records[client_ip] = calls

        return func(*args, **kwargs)
    return wrapper


@app.route('/search')  # 알라딘 검색
@rate_limit
def search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({'error': '검색어를 입력하세요.'}), 400

    cache_key = f"search:{query}"
    cached_result = cache.get(cache_key)
    if cached_result:
        return jsonify(cached_result)

    url = 'https://www.aladin.co.kr/ttb/api/ItemSearch.aspx'
    params = {
        'TTBKey': ALADIN_API_KEY,
        'Query': query,
        'QueryType': 'Keyword',
        'MaxResults': 24,
        'Start': 1,
        'SearchTarget': 'Book',
        'output': 'js',
        'Version': '20131101'
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        print("호출 URL:", resp.url)
        print("응답 내용:", resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
        items = data.get('item', [])

        result_items = []
        for book in items:
            result_items.append({
                'title': book.get('title'),
                'author': book.get('author'),
                'publisher': book.get('publisher'),
                'pubDate': book.get('pubDate'),
                'cover': book.get('cover'),
                'priceStandard': book.get('priceStandard'),
                'priceSales': book.get('priceSales'),
                'link': book.get('link'),
                'isbn13': book.get('isbn13') or book.get('isbn'),
            })

        cache.set(cache_key, {'item': result_items}, timeout=300)  # 5분 캐싱

        return jsonify({'item': result_items})

    except Exception as e:
        return jsonify({'error': f'알라딘 API 호출 실패: {str(e)}'}), 500


@app.route('/check_library')  # 선택 도서관의 소장과 대출 여부 확인
@rate_limit
def check_library():
    isbn = request.args.get('isbn', '').strip()
    library_code = request.args.get('libraryCode', '').strip()

    if not isbn or not library_code:
        return jsonify({'error': 'ISBN과 libraryCode를 모두 전달해야 합니다.'}), 400

    cache_key = f"check_library:{isbn}:{library_code}"
    cached_result = cache.get(cache_key)
    if cached_result:
        return jsonify(cached_result)

    all_libraries = load_library_data()
    target_library = next((lib for lib in all_libraries if lib['libraryCode'] == library_code), None)

    if not target_library:
        return jsonify({'error': '해당 libraryCode에 해당하는 도서관이 없습니다.'}), 404

    url = 'http://data4library.kr/api/bookExist'
    params = {
        'authKey': LIBRARY_API_KEY,
        'libCode': target_library['libraryCode'],
        'isbn13': isbn,
        'format': 'json'
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        print("API 응답 데이터:", data)

        result = data.get('response', {}).get('result', {})
        has_book = result.get('hasBook', 'N') == 'Y'
        loan_available = result.get('loanAvailable', 'N') == 'Y'

        response_data = {
            'results': [{
                'libraryName': target_library['libraryName'],
                'hasBook': has_book,
                'loanAvailable': loan_available
            }]
        }

        cache.set(cache_key, response_data, timeout=300)  # 5분 캐싱

        return jsonify(response_data)

    except Exception as e:
        return jsonify({
            'results': [{
                'libraryName': target_library['libraryName'],
                'error': "도서 정보 조회 실패"
            }]
        })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))  # Render에서 PORT 환경변수를 제공함
#  app.run(host='0.0.0.0', port=port)
    app.run(debug=True, port=5000)

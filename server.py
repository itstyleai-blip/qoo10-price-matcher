"""
server.py - Qoo10 최저가 매칭 시스템 백엔드
Flask + Playwright로 Qoo10 카탈로그 실제 스크래핑
"""
import asyncio
import json
import re
import os
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from contextlib import contextmanager
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# ============================================================
# DATABASE
# ============================================================
DB_PATH = 'data/price_history.db'
os.makedirs('data', exist_ok=True)

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_no INTEGER NOT NULL,
                seller_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                rank INTEGER,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS price_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_no INTEGER NOT NULL,
                old_price INTEGER,
                new_price INTEGER,
                reason TEXT,
                applied BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_snap ON price_snapshots(catalog_no, scraped_at);
            CREATE INDEX IF NOT EXISTS idx_chg ON price_changes(catalog_no, created_at);
        """)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ============================================================
# SCRAPER - Playwright (async)
# ============================================================
_browser = None
_playwright = None
_loop = None
_lock = threading.Lock()

def get_event_loop():
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop

async def _init_browser():
    global _browser, _playwright
    if _browser is None:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        print("[OK] Playwright 브라우저 초기화 완료")

async def _scrape_catalog(catalog_no):
    """Qoo10 카탈로그 페이지에서 셀러별 가격 추출"""
    await _init_browser()

    url = f"https://www.qoo10.jp/gmkt.inc/catalog/goods/goods.aspx?catalogno={catalog_no}&ga_priority=-1&ga_prdlist=srp"
    print(f"[SCRAPE] #{catalog_no} 스크래핑 시작: {url}")

    page = await _browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    sellers = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)

        # 페이지 로딩 대기 (동적 콘텐츠)
        await page.wait_for_timeout(3000)

        # 방법 1: JavaScript로 셀러 리스트 추출
        sellers = await page.evaluate("""
            () => {
                const results = [];

                // Qoo10 카탈로그 셀러 리스트 - 다양한 셀렉터 시도
                const selectors = [
                    // 카탈로그 상품 리스트
                    '.catalog_goods_list .item, .catalog_goods_list li',
                    '.cata_goods_list .item, .cata_goods_list li',
                    '#catalogGoodsList li, #catalog_goods_list li',
                    // 테이블 형식
                    '.tbl_catalog_goods tbody tr',
                    '.catalog_seller_list tr',
                    // 일반적인 상품 리스트
                    '.goods_list li, .goods_list .item',
                    '.prd_list li, .prd_list .item',
                    // 가격 비교 영역
                    '[class*="catalog"] [class*="item"]',
                    '[class*="catalog"] [class*="goods"]',
                    '[id*="catalog"] li',
                ];

                for (const sel of selectors) {
                    const items = document.querySelectorAll(sel);
                    if (items.length === 0) continue;

                    items.forEach((item) => {
                        // 가격 추출
                        let price = 0;
                        const priceEls = item.querySelectorAll(
                            '[class*="price"] em, [class*="price"] strong, [class*="prc"] em, ' +
                            '.price em, .prc em, .sell_price em, .sale_price em, strong.price, ' +
                            '[class*="Price"], em.price, .txt_price em'
                        );
                        for (const el of priceEls) {
                            const txt = el.textContent.replace(/[^0-9]/g, '');
                            const p = parseInt(txt);
                            if (p > 50 && p < 500000) { price = p; break; }
                        }
                        // 대체: 엔 기호 포함 텍스트
                        if (!price) {
                            const allText = item.textContent;
                            const m = allText.match(/([\d,]+)\s*円/) || allText.match(/¥\s*([\d,]+)/);
                            if (m) price = parseInt(m[1].replace(/,/g, ''));
                        }

                        // 셀러명 추출
                        let name = '';
                        const nameEls = item.querySelectorAll(
                            '[class*="seller"], [class*="shop"], .seller_name, .shop_name, ' +
                            'a[href*="shop"], a[href*="seller"], [class*="Seller"], [class*="Shop"]'
                        );
                        for (const el of nameEls) {
                            const n = el.textContent.trim();
                            if (n.length > 0 && n.length < 50) { name = n; break; }
                        }

                        // 상품 링크에서 상품코드 추출
                        let itemCode = '';
                        const links = item.querySelectorAll('a[href]');
                        for (const l of links) {
                            const match = l.href.match(/goodscode=([A-Za-z0-9]+)/i) ||
                                          l.href.match(/g\/([A-Za-z0-9]+)/i);
                            if (match) { itemCode = match[1]; break; }
                        }

                        if (price > 0) {
                            results.push({ name: name || '알수없음', price, itemCode });
                        }
                    });

                    if (results.length > 0) break;
                }

                // 방법 2: 전체 페이지에서 가격 패턴 스캔 (fallback)
                if (results.length === 0) {
                    // 스크립트 태그에서 JSON 데이터 탐색
                    const scripts = document.querySelectorAll('script');
                    for (const script of scripts) {
                        const text = script.textContent;
                        // Qoo10의 내부 데이터 구조 검색
                        const patterns = [
                            /"[Ss]eller[Nn]ame"\s*:\s*"([^"]+)"[^}]*"[Pp]rice"\s*:\s*["]?(\d+)/g,
                            /"[Ss]hop[Nn]ame"\s*:\s*"([^"]+)"[^}]*"[Ss]ell[Pp]rice"\s*:\s*["]?(\d+)/g,
                        ];
                        for (const pat of patterns) {
                            let m;
                            while ((m = pat.exec(text)) !== null) {
                                results.push({ name: m[1], price: parseInt(m[2]), itemCode: '' });
                            }
                        }
                        if (results.length > 0) break;
                    }
                }

                // 방법 3: 모든 가격 표시 요소 수집
                if (results.length === 0) {
                    const allPriceEls = document.querySelectorAll(
                        '.goods_price, .item_price, [class*="goods_price"], [class*="item_price"]'
                    );
                    allPriceEls.forEach((el, idx) => {
                        const txt = el.textContent.replace(/[^0-9]/g, '');
                        const p = parseInt(txt);
                        if (p > 50 && p < 500000) {
                            // 부모에서 셀러명 찾기
                            let parent = el.closest('li, tr, div[class*="item"], div[class*="goods"]');
                            let name = '';
                            if (parent) {
                                const nameEl = parent.querySelector('[class*="seller"], [class*="shop"]');
                                if (nameEl) name = nameEl.textContent.trim();
                            }
                            results.push({ name: name || `셀러${idx+1}`, price: p, itemCode: '' });
                        }
                    });
                }

                return results;
            }
        """)

        # 방법 4: Qoo10 내부 AJAX API 시도
        if not sellers:
            print(f"[SCRAPE] #{catalog_no} JS 추출 실패, AJAX API 시도...")
            try:
                api_result = await page.evaluate(f"""
                    async () => {{
                        try {{
                            const resp = await fetch(
                                '/GMKT.INC/Catalog/CatalogHandler.ashx?method=GetCatalogSellerList&catalogNo={catalog_no}',
                                {{ credentials: 'include' }}
                            );
                            return await resp.text();
                        }} catch(e) {{
                            return null;
                        }}
                    }}
                """)
                if api_result and len(api_result) > 5:
                    try:
                        data = json.loads(api_result)
                        items = data if isinstance(data, list) else data.get('Items', data.get('ResultObject', []))
                        for item in items:
                            name = item.get('SellerName') or item.get('ShopName') or 'unknown'
                            price = item.get('Price') or item.get('SellPrice') or item.get('SellingPrice', 0)
                            if isinstance(price, str):
                                price = int(price.replace(',', ''))
                            if price > 0:
                                sellers.append({'name': name, 'price': int(price), 'itemCode': item.get('GoodsCode', '')})
                    except json.JSONDecodeError:
                        pass
            except Exception as e:
                print(f"[SCRAPE] AJAX API 에러: {e}")

        # 방법 5: 페이지 HTML에서 정규식으로 추출
        if not sellers:
            print(f"[SCRAPE] #{catalog_no} AJAX도 실패, HTML 정규식 시도...")
            html = await page.content()

            # Qoo10의 data-* 속성이나 인라인 데이터에서 가격 추출
            price_patterns = [
                r'"goodsPrice"\s*:\s*"?(\d+)"?.*?"sellerNick"\s*:\s*"([^"]+)"',
                r'"sellerNick"\s*:\s*"([^"]+)".*?"goodsPrice"\s*:\s*"?(\d+)"?',
                r'data-price="(\d+)"[^>]*data-seller="([^"]+)"',
            ]
            for pat in price_patterns:
                for m in re.finditer(pat, html, re.DOTALL):
                    groups = m.groups()
                    if groups[0].isdigit():
                        sellers.append({'name': groups[1], 'price': int(groups[0]), 'itemCode': ''})
                    else:
                        sellers.append({'name': groups[0], 'price': int(groups[1]), 'itemCode': ''})

        # 스크린샷 저장 (디버깅용)
        if not sellers:
            screenshot_path = f"data/debug_{catalog_no}.png"
            await page.screenshot(path=screenshot_path, full_page=True)
            print(f"[SCRAPE] #{catalog_no} 데이터 없음. 스크린샷: {screenshot_path}")

            # 마지막 시도: 페이지 텍스트에서 가격 패턴
            text_content = await page.evaluate("() => document.body.innerText")
            lines = text_content.split('\n')
            for line in lines:
                # "셀러명    ¥1,234" 또는 "1,234円" 패턴
                m = re.search(r'¥\s*([\d,]+)', line) or re.search(r'([\d,]+)\s*円', line)
                if m:
                    price = int(m.group(1).replace(',', ''))
                    if 100 < price < 100000:
                        # 같은 줄에서 셀러명 후보
                        name_part = line[:line.find(m.group(0))].strip()
                        if not name_part:
                            name_part = f"셀러"
                        sellers.append({'name': name_part[:30], 'price': price, 'itemCode': ''})

        print(f"[SCRAPE] #{catalog_no} 결과: {len(sellers)}개 셀러")
        return sellers

    except Exception as e:
        print(f"[ERROR] #{catalog_no} 스크래핑 에러: {e}")
        # 에러 시에도 스크린샷
        try:
            await page.screenshot(path=f"data/error_{catalog_no}.png")
        except:
            pass
        return []
    finally:
        await page.close()


def scrape_catalog_sync(catalog_no):
    """동기 래퍼"""
    with _lock:
        loop = get_event_loop()
        return loop.run_until_complete(_scrape_catalog(catalog_no))


# ============================================================
# API ROUTES
# ============================================================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/scrape/<int:catalog_no>')
def api_scrape(catalog_no):
    """단일 카탈로그 스크래핑"""
    try:
        sellers = scrape_catalog_sync(catalog_no)

        # 중복 제거 + 정렬
        seen = {}
        for s in sellers:
            key = s['name'].lower().strip()
            if key not in seen or s['price'] < seen[key]['price']:
                seen[key] = s
        sellers = sorted(seen.values(), key=lambda x: x['price'])

        # 순위 부여
        for i, s in enumerate(sellers, 1):
            s['rank'] = i

        # DB 저장
        if sellers:
            with get_db() as conn:
                for s in sellers:
                    conn.execute(
                        "INSERT INTO price_snapshots (catalog_no, seller_name, price, rank) VALUES (?,?,?,?)",
                        (catalog_no, s['name'], s['price'], s['rank'])
                    )

        return jsonify({'success': True, 'sellers': sellers, 'count': len(sellers)})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'sellers': []})


@app.route('/api/scrape-all', methods=['POST'])
def api_scrape_all():
    """여러 카탈로그 일괄 스크래핑"""
    data = request.json
    catalogs = data.get('catalogs', [])
    results = {}

    for cat in catalogs:
        catalog_no = cat.get('catalogNo')
        if not catalog_no:
            continue
        try:
            sellers = scrape_catalog_sync(catalog_no)

            seen = {}
            for s in sellers:
                key = s['name'].lower().strip()
                if key not in seen or s['price'] < seen[key]['price']:
                    seen[key] = s
            sellers = sorted(seen.values(), key=lambda x: x['price'])
            for i, s in enumerate(sellers, 1):
                s['rank'] = i

            if sellers:
                with get_db() as conn:
                    for s in sellers:
                        conn.execute(
                            "INSERT INTO price_snapshots (catalog_no, seller_name, price, rank) VALUES (?,?,?,?)",
                            (catalog_no, s['name'], s['price'], s['rank'])
                        )

            results[catalog_no] = {'success': True, 'sellers': sellers}
            time.sleep(2)  # 요청 간격

        except Exception as e:
            results[catalog_no] = {'success': False, 'error': str(e), 'sellers': []}

    return jsonify(results)


@app.route('/api/history/<int:catalog_no>')
def api_history(catalog_no):
    """가격 이력 조회"""
    days = request.args.get('days', 7, type=int)
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM price_changes WHERE catalog_no=? AND created_at>=? ORDER BY created_at DESC",
            (catalog_no, since)
        ).fetchall()
        return jsonify([dict(r) for r in rows])


@app.route('/api/price-change', methods=['POST'])
def api_price_change():
    """가격 변경 기록"""
    data = request.json
    with get_db() as conn:
        conn.execute(
            "INSERT INTO price_changes (catalog_no, old_price, new_price, reason, applied) VALUES (?,?,?,?,?)",
            (data['catalogNo'], data.get('oldPrice'), data['newPrice'], data.get('reason', ''), data.get('applied', False))
        )
    return jsonify({'success': True})


@app.route('/api/recent-drops/<int:catalog_no>')
def api_recent_drops(catalog_no):
    """24시간 내 인하 횟수"""
    since = (datetime.now() - timedelta(hours=24)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM price_changes WHERE catalog_no=? AND applied=1 AND new_price<old_price AND created_at>=?",
            (catalog_no, since)
        ).fetchone()
        return jsonify({'count': row['cnt'] if row else 0})


@app.route('/api/debug/<int:catalog_no>')
def api_debug(catalog_no):
    """디버그 스크린샷 조회"""
    for prefix in ['debug', 'error']:
        path = f"data/{prefix}_{catalog_no}.png"
        if os.path.exists(path):
            return send_from_directory('data', f"{prefix}_{catalog_no}.png")
    return jsonify({'error': 'no screenshot'}), 404


# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    init_db()
    print("\n" + "="*50)
    print("  Qoo10 최저가 매칭 시스템 서버")
    print("  http://localhost:5000")
    print("="*50 + "\n")

    # 1.5초 후 브라우저 자동 열기
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()

    app.run(host='0.0.0.0', port=5000, debug=False)

"""
server.py - Qoo10 최저가 매칭 시스템 백엔드 v2
Flask + Playwright로 Qoo10 카탈로그 실제 스크래핑

Qoo10 카탈로그 페이지 구조 (2026.02 기준):
  - "公式ショップ" 섹션: 공식 셀러
  - "ショップ（送料込みの価額が安い順）" 섹션: 전체 셀러 리스트
  - 각 셀러 행: [公式] 셀러명 | メガポ時 가격円 | 送料無料
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

DB_PATH = 'data/price_history.db'
os.makedirs('data', exist_ok=True)

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_no INTEGER NOT NULL, seller_name TEXT NOT NULL,
                price INTEGER NOT NULL, rank INTEGER,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS price_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_no INTEGER NOT NULL, old_price INTEGER, new_price INTEGER,
                reason TEXT, applied BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try: yield conn; conn.commit()
    finally: conn.close()

# ============================================================
# SCRAPER
# ============================================================
_browser = None
_playwright = None
_lock = threading.Lock()

async def _init_browser():
    global _browser, _playwright
    if _browser is None:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        print("[OK] Playwright 브라우저 초기화 완료")

async def _scrape_catalog(catalog_no):
    """Qoo10 카탈로그 페이지에서 셀러별 가격 추출"""
    await _init_browser()
    url = f"https://www.qoo10.jp/gmkt.inc/catalog/goods/goods.aspx?catalogno={catalog_no}"
    print(f"\n[SCRAPE] #{catalog_no} 시작: {url}")

    page = await _browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    sellers = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)

        # 스크린샷 저장 (디버그)
        await page.screenshot(path=f"data/page_{catalog_no}.png", full_page=True)

        # ============================================
        # 방법 1: 페이지 텍스트 기반 파싱
        # ============================================
        page_text = await page.evaluate("() => document.body.innerText")
        with open(f"data/text_{catalog_no}.txt", "w", encoding="utf-8") as f:
            f.write(page_text)
        print(f"[DEBUG] 텍스트 길이: {len(page_text)}")

        # "ショップ（送料込み" 섹션 찾기
        shop_idx = -1
        for marker in ["ショップ（送料込み", "ショップ(送料込み"]:
            shop_idx = page_text.find(marker)
            if shop_idx >= 0:
                break

        if shop_idx >= 0:
            section = page_text[shop_idx:]
            print(f"[DEBUG] 셀러 섹션 발견")
        else:
            section = page_text
            print(f"[DEBUG] 셀러 섹션 마커 미발견, 전체 텍스트 사용")

        # 줄 단위 파싱
        lines = [l.strip() for l in section.split('\n') if l.strip()]

        # 셀러 이름 후보를 모아두고, 바로 다음에 나오는 가격과 매칭
        # Qoo10 구조: 셀러명 → (メガポ時) → 가격円 → 送料
        seller_name_candidate = ""
        for line in lines:
            # 가격 패턴: "2,200円" 또는 "2,444円"
            price_match = re.search(r'^([\d,]+)\s*円', line) or re.search(r'([\d,]+)\s*円', line)

            if price_match:
                price = int(price_match.group(1).replace(',', ''))
                if 100 <= price <= 500000 and seller_name_candidate:
                    # 중복 방지
                    if not any(s['name'] == seller_name_candidate and s['price'] == price for s in sellers):
                        sellers.append({'name': seller_name_candidate, 'price': price, 'itemCode': ''})
                        print(f"  [발견] {seller_name_candidate}: ¥{price:,}")
                    seller_name_candidate = ""
                continue

            # メガポ時, 送料, ショップ割 등은 건너뜀
            skip_words = ['メガポ時', 'ショップ割', 'Q-ONLY', '送料無料', '送料有料',
                          '公式ショップ', 'ショップ（', '全クーポン', '最安値', 'TOP',
                          '比較リスト', 'シェア', 'お気に入り', 'ブランド', 'レビュー',
                          '件の', '保湿', 'テクスチャー', 'ボディクリ', 'ボディケア',
                          'ビューティー', 'カテゴリ', '検索', 'ログイン', 'カート',
                          'ヘルプ', 'ランキング', 'タイムセール', '円~', '円～']
            if any(w in line for w in skip_words):
                continue

            # 숫자만으로 된 줄 건너뜀
            if re.match(r'^[\d,.\s]+$', line):
                continue

            # 짧은 텍스트(1글자) 건너뜀
            if len(line) <= 1:
                continue

            # 이것이 셀러명 후보
            # "公式" 태그 제거
            clean = re.sub(r'^公式\s*', '', line).strip()
            if clean and 2 <= len(clean) <= 40:
                seller_name_candidate = clean

        # ============================================
        # 방법 2: "公式ショップ" 섹션도 별도 파싱
        # ============================================
        official_idx = page_text.find("公式ショップ")
        if official_idx >= 0 and shop_idx >= 0:
            official_section = page_text[official_idx:shop_idx]
            off_lines = [l.strip() for l in official_section.split('\n') if l.strip()]
            off_name = ""
            for line in off_lines:
                price_match = re.search(r'([\d,]+)\s*円', line)
                if price_match:
                    price = int(price_match.group(1).replace(',', ''))
                    if 100 <= price <= 500000 and off_name:
                        if not any(s['name'] == off_name for s in sellers):
                            sellers.append({'name': off_name, 'price': price, 'itemCode': ''})
                            print(f"  [공식] {off_name}: ¥{price:,}")
                    continue
                clean = re.sub(r'^公式\s*', '', line).strip()
                skip = ['メガポ時', '送料', 'ショップ割', 'Q-ONLY', '公式ショップ']
                if not any(w in clean for w in skip) and 2 <= len(clean) <= 40:
                    off_name = clean

        # ============================================
        # 방법 3: DOM에서 직접 추출 (방법1,2 실패 시)
        # ============================================
        if not sellers:
            print("[DEBUG] 텍스트 파싱 실패, DOM 탐색...")
            sellers = await page.evaluate("""
                () => {
                    const results = [];
                    // 모든 요소에서 가격+셀러 패턴 찾기
                    const allEls = document.querySelectorAll('div, li, tr, section, article');
                    for (const el of allEls) {
                        if (el.children.length > 10) continue; // 너무 큰 컨테이너 스킵
                        const text = el.innerText || '';
                        if (text.length > 300) continue;
                        
                        const priceMatch = text.match(/([\d,]+)\s*円/);
                        if (!priceMatch) continue;
                        const price = parseInt(priceMatch[1].replace(/,/g, ''));
                        if (price < 100 || price > 500000) continue;
                        
                        // 셀러명 추출: 가격/송료 등 제거
                        let name = text
                            .replace(/メガポ時|ショップ割|Q-ONLY|公式/g, '')
                            .replace(/([\d,]+)\s*円/g, '')
                            .replace(/送料無料|送料有料/g, '')
                            .replace(/[\\n\\r\\t]+/g, ' ')
                            .trim();
                        
                        if (name.length >= 2 && name.length <= 35) {
                            if (!results.some(r => r.name === name && r.price === price)) {
                                results.push({ name, price, itemCode: '' });
                            }
                        }
                    }
                    return results;
                }
            """)

        # 결과 정리
        if sellers:
            seen = {}
            for s in sellers:
                key = s['name'].strip()
                if key not in seen or s['price'] < seen[key]['price']:
                    seen[key] = s
            sellers = sorted(seen.values(), key=lambda x: x['price'])
            for i, s in enumerate(sellers, 1):
                s['rank'] = i
            print(f"\n[결과] #{catalog_no}: {len(sellers)}개 셀러")
            for s in sellers:
                print(f"  {s['rank']}위 {s['name']}: ¥{s['price']:,}")
        else:
            print(f"\n[실패] #{catalog_no}: 셀러 없음")
            print(f"  디버그: data/page_{catalog_no}.png / data/text_{catalog_no}.txt")

        return sellers

    except Exception as e:
        print(f"[ERROR] #{catalog_no}: {e}")
        try: await page.screenshot(path=f"data/error_{catalog_no}.png")
        except: pass
        return []
    finally:
        await page.close()

def scrape_catalog_sync(catalog_no):
    with _lock:
        loop = asyncio.new_event_loop()
        try: return loop.run_until_complete(_scrape_catalog(catalog_no))
        finally: loop.close()

# ============================================================
# API ROUTES
# ============================================================
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/scrape/<int:catalog_no>')
def api_scrape(catalog_no):
    try:
        sellers = scrape_catalog_sync(catalog_no)
        if sellers:
            with get_db() as conn:
                for s in sellers:
                    conn.execute("INSERT INTO price_snapshots (catalog_no,seller_name,price,rank) VALUES (?,?,?,?)",
                        (catalog_no, s['name'], s['price'], s['rank']))
        return jsonify({'success': True, 'sellers': sellers, 'count': len(sellers)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'sellers': []})

@app.route('/api/scrape-all', methods=['POST'])
def api_scrape_all():
    data = request.json
    results = {}
    for cat in data.get('catalogs', []):
        cno = cat.get('catalogNo')
        if not cno: continue
        try:
            sellers = scrape_catalog_sync(cno)
            if sellers:
                with get_db() as conn:
                    for s in sellers:
                        conn.execute("INSERT INTO price_snapshots (catalog_no,seller_name,price,rank) VALUES (?,?,?,?)",
                            (cno, s['name'], s['price'], s['rank']))
            results[cno] = {'success': True, 'sellers': sellers}
            time.sleep(2)
        except Exception as e:
            results[cno] = {'success': False, 'error': str(e), 'sellers': []}
    return jsonify(results)

@app.route('/api/history/<int:catalog_no>')
def api_history(catalog_no):
    days = request.args.get('days', 7, type=int)
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM price_changes WHERE catalog_no=? AND created_at>=? ORDER BY created_at DESC",
            (catalog_no, since)).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/price-change', methods=['POST'])
def api_price_change():
    data = request.json
    with get_db() as conn:
        conn.execute("INSERT INTO price_changes (catalog_no,old_price,new_price,reason,applied) VALUES (?,?,?,?,?)",
            (data['catalogNo'], data.get('oldPrice'), data['newPrice'], data.get('reason',''), data.get('applied',False)))
    return jsonify({'success': True})

@app.route('/api/debug/<int:catalog_no>')
def api_debug(catalog_no):
    ft = request.args.get('type', 'png')
    if ft == 'txt':
        path = f"data/text_{catalog_no}.txt"
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read(), 200, {'Content-Type': 'text/plain; charset=utf-8'}
    else:
        for prefix in ['page', 'error']:
            path = f"data/{prefix}_{catalog_no}.png"
            if os.path.exists(path):
                return send_from_directory('data', f"{prefix}_{catalog_no}.png")
    return jsonify({'error': 'not found'}), 404

# ============================================================
if __name__ == '__main__':
    init_db()
    print("\n" + "="*50)
    print("  🏷️  Qoo10 최저가 매칭 시스템 서버 v2")
    print("  📡 http://localhost:5000")
    print("="*50)
    print("  디버그: data/page_{번호}.png, data/text_{번호}.txt")
    print("="*50 + "\n")
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()
    app.run(host='0.0.0.0', port=5000, debug=False)

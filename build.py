#!/usr/bin/env python3
"""
내 뉴스 — 취향 맞춤 뉴스 클리핑 빌더 (GitHub Actions에서 실행)

1) 국내/해외 × 5개 카테고리 × 세부 카테고리별로 Google News RSS(사이트 지정 검색)에서 기사 목록 수집
2) 본문을 가져올 수 있는 기사는 본문을 추출해 'AES-GCM으로 암호화'해서 저장
   (공개 저장소에 기사 전문이 그대로 올라가지 않도록. 암호는 GitHub Secret READER_KEY)
3) site/ 폴더에 앱(index.html)과 데이터(data/)를 만들어 GitHub Pages로 배포
"""
import base64
import concurrent.futures as cf
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.parse
from collections import OrderedDict

import feedparser

socket.setdefaulttimeout(20)

OUT = "site"
PER_SUB = 5                 # 세부 카테고리 × 지역당 기사 수
BODY_BUDGET_SEC = 11 * 60   # 본문 추출에 쓸 최대 시간
WORKERS = 6
MIN_CHARS = 400             # 이보다 짧으면 본문 수집 실패(유료 벽·요약만 제공)로 판단
MAX_CHARS = 24000
KDF_ITER = 200_000
READER_KEY = os.environ.get("READER_KEY", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ── 주요 언론사 ────────────────────────────────────────────────
GENERAL = {
    "kr": ["chosun.com", "donga.com", "joongang.co.kr"],
    "gl": ["thetimes.com", "thetimes.co.uk", "nytimes.com", "bbc.com", "bbc.co.uk",
           "washingtonpost.com", "theguardian.com"],
}
# 유료 구독 기사는 본문을 가져오지 않고 '새 창 열기'로 처리 (유료 벽을 우회하지 않음)
SKIP_BODY = ("nytimes.com", "thetimes.com", "thetimes.co.uk", "washingtonpost.com", "economist.com",
             "newyorker.com", "lrb.co.uk", "nybooks.com", "businessoffashion.com", "ft.com", "wsj.com")

# ── 카테고리 · 세부 카테고리 · 전문매체 ───────────────────────────
# subs: (세부명, 한글 검색어, 영문 검색어)  ※ 여러 단어 용어는 자동으로 따옴표 처리됨
TAX = [
    {"id": "politics", "name": "정치", "days": 2,
     "extra": {"kr": ["yna.co.kr", "sisain.co.kr"], "gl": ["politico.com", "economist.com"]},
     "subs": [
         ("국내정치", "국회 OR 여야 OR 정당 OR 국정", "politics OR Congress OR Parliament OR Senate OR Westminster"),
         ("총선", "총선 OR 국회의원 선거 OR 공천", "general election OR parliamentary election OR midterm elections"),
         ("대선", "대선 OR 대통령 선거 OR 대선주자", "presidential election OR presidential race"),
         ("오피니언", "사설 OR 칼럼 OR 시론", "opinion OR editorial OR op-ed"),
     ]},
    {"id": "tech", "name": "AI/전자통신", "days": 3,
     "extra": {"kr": ["etnews.com", "bloter.net", "aitimes.com", "zdnet.co.kr"],
               "gl": ["theverge.com", "wired.com", "techcrunch.com", "arstechnica.com", "technologyreview.com"]},
     "subs": [
         ("인공지능", "인공지능 OR AI", "artificial intelligence OR AI"),
         ("생성형 AI", "생성형 AI OR 챗GPT OR 제미나이 OR 클로드 OR LLM", "generative AI OR ChatGPT OR Gemini OR Claude OR LLM"),
         ("스마트폰", "스마트폰 OR 갤럭시 OR 아이폰", "smartphone OR iPhone OR Galaxy OR Pixel"),
         ("전자기기", "노트북 OR 태블릿 OR 웨어러블 OR 전자기기", "laptop OR tablet OR gadget OR wearable OR headphones"),
     ]},
    {"id": "life", "name": "라이프스타일", "days": 7,
     "extra": {"kr": ["kormedi.com", "medicaltimes.com", "docdocdoc.co.kr", "bosa.co.kr", "doctorsnews.co.kr",
                      "hidoc.co.kr", "fashionbiz.co.kr", "apparelnews.co.kr", "vogue.co.kr", "elle.co.kr"],
               "gl": ["health.harvard.edu", "healthline.com", "aarp.org", "vogue.com", "businessoffashion.com"]},
     "subs": [
         ("패션", "패션 OR 의류 OR 컬렉션", "fashion OR clothing OR collection"),
         ("중년건강", "중년 건강 OR 갱년기 OR 노년 건강 OR 근감소증", "midlife health OR menopause OR healthy ageing OR over 50"),
         ("생활팁", "생활 꿀팁 OR 생활정보 OR 살림 OR 절약", "life hacks OR household tips OR home tips"),
         ("건강정보", "건강 OR 질환 OR 의학", "health OR medicine OR wellbeing"),
         ("패션트렌드", "패션 트렌드 OR 유행 OR 스타일링", "fashion trends OR street style OR style trend"),
         ("건강식단", "건강 식단 OR 영양 OR 식이요법", "healthy diet OR nutrition OR Mediterranean diet OR meal plan"),
         ("인문학", "인문학 OR 철학 OR 역사", "humanities OR philosophy OR history OR culture essay"),
     ]},
    {"id": "travel", "name": "여행", "days": 14,
     "extra": {"kr": ["traveltimes.co.kr", "traveldaily.co.kr"],
               "gl": ["lonelyplanet.com", "cntraveler.com", "travelandleisure.com", "nationalgeographic.com",
                      "outsideonline.com", "caminoways.com"]},
     "subs": [
         ("국내여행", "국내여행 OR 여행지 OR 지역 축제", "South Korea travel OR Seoul OR Jeju OR Busan"),
         ("도보여행", "도보여행 OR 걷기 여행 OR 둘레길", "walking holiday OR walking tour OR long-distance walk"),
         ("기차여행", "기차여행 OR 열차 여행 OR 철도 여행 OR 관광열차", "train travel OR rail journey OR scenic railway OR sleeper train"),
         ("일본여행", "일본여행 OR 일본 여행", "Japan travel OR travel to Japan OR Japan trip"),
         ("미국여행", "미국여행 OR 미국 여행", "USA travel OR US road trip OR national parks"),
         ("산티아고순례길", "산티아고 순례길 OR 카미노", "Camino de Santiago OR Camino pilgrimage"),
         ("세계일주", "세계일주 OR 장기 여행 OR 한달살기", "round the world trip OR long-term travel OR digital nomad OR backpacking"),
         ("남미여행", "남미여행 OR 남미 여행 OR 파타고니아 OR 페루", "South America travel OR Patagonia OR Peru travel OR Machu Picchu"),
         ("아프리카여행", "아프리카 여행 OR 사파리 OR 킬리만자로", "Africa travel OR safari OR Kilimanjaro OR Morocco travel"),
         ("등산", "등산 OR 산행 OR 명산", "mountaineering OR mountain hiking OR climbing"),
         ("트래킹", "트래킹 OR 트레킹 OR 히말라야", "trekking OR Himalaya trek OR long hike"),
     ]},
    {"id": "lit", "name": "문학", "days": 21,
     "extra": {"kr": ["ch.yes24.com", "readersnews.com", "bookdaily.co.kr"],
               "gl": ["theparisreview.org", "lithub.com", "lrb.co.uk", "nybooks.com", "newyorker.com", "aeon.co"]},
     "subs": [
         ("세계문학", "세계문학 OR 해외 문학 OR 노벨문학상", "world literature OR translated fiction OR Nobel Prize in Literature"),
         ("고전", "고전 문학 OR 고전 읽기 OR 고전", "classic literature OR classics OR classic novel"),
         ("소설", "소설 OR 장편소설 OR 신간", "novel OR fiction OR new novel"),
         ("시집", "시집 OR 시인 OR 신작 시", "poetry OR poet OR poetry collection"),
         ("에세이", "에세이 OR 산문집", "essay OR essays OR memoir"),
         ("철학", "철학 OR 철학자 OR 사상", "philosophy OR philosopher"),
     ]},
]
REGIONS = ("kr", "gl")


# ── 수집 ────────────────────────────────────────────────────────
def gnews_url(domains, kw, days, region):
    sites = " OR ".join(f"site:{d}" for d in domains)
    terms = " OR ".join(f'"{t}"' if " " in t else t for t in (x.strip() for x in kw.split(" OR ")))
    q = f"({sites}) ({terms}) when:{days}d"
    p = ({"hl": "ko", "gl": "KR", "ceid": "KR:ko"} if region == "kr"
         else {"hl": "en-US", "gl": "US", "ceid": "US:en"})
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, **p})


def fetch_feed(url):
    try:
        return feedparser.parse(url, agent=UA).entries
    except Exception:  # noqa: BLE001
        return []


def src_of(e):
    s = e.get("source") or {}
    return (s.get("title") or "").strip(), (s.get("href") or "").strip()


def diversify(entries, n):
    """언론사별로 번갈아 뽑아 한 매체로 쏠리지 않게 한다."""
    groups = OrderedDict()
    for e in entries:
        groups.setdefault(src_of(e)[0], []).append(e)
    out = []
    while len(out) < n and any(groups.values()):
        for k in list(groups):
            if groups[k] and len(out) < n:
                out.append(groups[k].pop(0))
    return out


def iso(e):
    p = e.get("published_parsed") or e.get("updated_parsed")
    d = (datetime.datetime(*p[:6]) if p else datetime.datetime.utcnow())
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def collect():
    arts, stats = {}, {}
    for region in REGIONS:
        for cat in TAX:
            domains = GENERAL[region] + cat["extra"][region]
            for sub, kw_ko, kw_en in cat["subs"]:
                kw = kw_ko if region == "kr" else kw_en
                entries = fetch_feed(gnews_url(domains, kw, cat["days"], region))
                time.sleep(0.5)
                picks = diversify(entries, PER_SUB)
                stats[(region, cat["id"], sub)] = len(picks)
                for e in picks:
                    link = e.get("link", "")
                    title = (e.get("title") or "").strip()
                    name, href = src_of(e)
                    if name:
                        title = re.sub(r"\s+[-–—]\s+" + re.escape(name) + r"\s*$", "", title)
                    if not link or not title:
                        continue
                    aid = hashlib.sha1(link.encode()).hexdigest()[:10]
                    a = arts.setdefault(aid, {"id": aid, "t": title, "o": name or "출처 미상", "r": region,
                                              "d": iso(e), "u": link, "h": href, "x": 0, "tags": []})
                    tag = f"{cat['id']}|{sub}"
                    if tag not in a["tags"]:
                        a["tags"].append(tag)
    return arts, stats


# ── 본문 추출 + 암호화 ───────────────────────────────────────────
_lock = threading.Lock()
_last = [0.0]


def decode_url(gurl):
    """Google News 링크 → 원문 주소 (서버 부하 방지를 위해 순차 + 간격)."""
    from googlenewsdecoder import gnewsdecoder
    with _lock:
        wait = 0.6 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        r = gnewsdecoder(gurl)
    return r.get("decoded_url") if r and r.get("status") else None


def fetch_text(url):
    import trafilatura
    html_ = trafilatura.fetch_url(url)
    if not html_:
        return None
    return trafilatura.extract(html_, include_comments=False, include_tables=False, favor_precision=True)


NOISE = re.compile(r"(무단\s*전재|재배포|Copyright|©|ⓒ|All rights reserved)", re.I)


def clean(text):
    paras = [p.strip() for p in (text or "").split("\n") if p.strip() and not NOISE.search(p)]
    body = "\n".join(paras)
    if len(body) < MIN_CHARS:
        return None
    return [p for p in paras][:200] if len(body) <= MAX_CHARS else _truncate(paras)


def _truncate(paras):
    out, n = [], 0
    for p in paras:
        n += len(p)
        if n > MAX_CHARS:
            break
        out.append(p)
    return out


def make_cipher(passphrase):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt = os.urandom(16)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITER).derive(passphrase.encode())
    aes = AESGCM(key)

    def enc(data: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + aes.encrypt(nonce, data, None)   # 브라우저 WebCrypto와 같은 형식(암호문+태그)
    return salt, enc


def extract_bodies(arts, enc):
    os.makedirs(f"{OUT}/data/a", exist_ok=True)
    deadline = time.time() + BODY_BUDGET_SEC
    todo = [a for a in arts.values() if not any(a["h"].endswith(d) or d in a["h"] for d in SKIP_BODY)]
    print(f"본문 추출 대상 {len(todo)}건 / 전체 {len(arts)}건 (유료 구독 매체 제외)")

    def work(a):
        if time.time() > deadline:
            return False
        try:
            real = decode_url(a["u"])
            if not real:
                return False
            if any(d in real for d in SKIP_BODY):
                a["u"] = real
                return False
            paras = clean(fetch_text(real))
            a["u"] = real            # 원문 직접 링크로 교체
            if not paras:
                return False
            data = json.dumps({"p": paras}, ensure_ascii=False).encode()
            with open(f"{OUT}/data/a/{a['id']}.bin", "wb") as f:
                f.write(enc(data))
            a["x"] = 1
            return True
        except Exception as ex:  # noqa: BLE001
            print("  본문 실패:", a["o"], type(ex).__name__)
            return False

    with cf.ThreadPoolExecutor(WORKERS) as ex:
        ok = sum(ex.map(work, todo))
    print(f"본문 수집 성공 {ok}건")


# ── 빌드 ────────────────────────────────────────────────────────
def main():
    os.makedirs(OUT, exist_ok=True)
    arts, stats = collect()
    print(f"기사 {len(arts)}건 수집")
    for (r, c, s), n in stats.items():
        if n == 0:
            print("  (0건)", r, c, s)

    salt_b64 = chk_b64 = ""
    if READER_KEY:
        salt, enc = make_cipher(READER_KEY)
        salt_b64 = base64.b64encode(salt).decode()
        chk_b64 = base64.b64encode(enc(b"ok")).decode()
        try:
            extract_bodies(arts, enc)
        except ImportError as ex:
            print("본문 추출 라이브러리 없음 → 모든 기사를 새 창으로 엽니다:", ex)
    else:
        print("READER_KEY 가 없어 본문 저장을 건너뜁니다(모든 기사 새 창 열기).")

    items = [{k: v for k, v in a.items() if k != "h"} for a in arts.values()]
    tax = [{"id": c["id"], "name": c["name"], "subs": [s[0] for s in c["subs"]]} for c in TAX]
    index = {"built": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "enc": bool(READER_KEY),
             "salt": salt_b64, "chk": chk_b64, "iter": KDF_ITER, "tax": tax, "items": items}
    os.makedirs(f"{OUT}/data", exist_ok=True)
    with open(f"{OUT}/data/index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    shutil.copy("index.html", f"{OUT}/index.html")
    for extra in ("icon.png",):
        if os.path.exists(extra):
            shutil.copy(extra, f"{OUT}/{extra}")
    print("완료:", f"{OUT}/")


if __name__ == "__main__":
    main()

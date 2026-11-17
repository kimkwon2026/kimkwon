"""
개인용 뉴스 앱 생성기
국내(중앙·조선·동아·전자신문·블로터) / 해외(BBC·NYT·WaPo) 기사를
여행·문학·건강·IT·정치·패션 주제별로 모아 site/index.html(국내), site/global.html(해외, 영어)로 만듭니다.
GitHub Actions가 매일 실행해서 GitHub Pages로 배포합니다.
"""
import datetime
import html
import json
import os
import re
import shutil
import socket
import time
import urllib.parse
import urllib.request

import feedparser

socket.setdefaulttimeout(15)
OUT = "site"
PER_OUTLET = 3  # 언론사당 주제별 기사 수
KST = datetime.timezone(datetime.timedelta(hours=9))

# id, 한글명, 이모지, 영문명, 한글 검색어, 영문 검색어, 최근 며칠
TOPICS = [
    ("travel", "여행", "🧳", "Travel", "여행 OR 여행지 OR 관광지 OR 해외여행", "travel OR tourism OR destination OR trip", 3),
    ("lit", "문학", "📖", "Books & Literature", "문학 OR 소설 OR 시집 OR 작가", "books OR novel OR literature OR author", 5),
    ("health", "건강", "🩺", "Health", "건강 OR 의학 OR 질병", "health OR medicine OR disease", 3),
    ("it", "IT·AI·통신", "💻", "Tech & AI", "AI OR 인공지능 OR 반도체 OR 통신 OR 스마트폰", "AI OR technology OR chips OR smartphone", 2),
    ("politics", "정치", "🏛", "Politics", "정치", "politics", 1),
    ("fashion", "패션", "👗", "Fashion", "패션 OR 뷰티 OR 디자이너", "fashion OR style OR designer", 5),
]

KO_OUTLETS = [("중앙일보", "joongang.co.kr"), ("조선일보", "chosun.com"), ("동아일보", "donga.com"),
              ("전자신문", "etnews.com"), ("블로터", "bloter.net")]
EN_OUTLETS = [("BBC", "bbc.com"), ("New York Times", "nytimes.com"), ("Washington Post", "washingtonpost.com")]
IT_ONLY = {"전자신문", "블로터"}  # 이 두 곳은 IT 주제에서만 사용

CHOSUN = "https://www.chosun.com/arc/outboundfeeds/rss/category/{}/?outputType=xml"
NYT = "https://rss.nytimes.com/services/xml/rss/nyt/{}.xml"
# 섹션 RSS가 확실한 곳은 직접 사용하고, 그 외·실패 시에는 Google News 사이트검색으로 대체
PRIMARY = {
    ("politics", "중앙일보"): "https://rss.joins.com/joins_politics_list.xml",
    ("politics", "조선일보"): CHOSUN.format("politics"),
    ("politics", "동아일보"): "https://rss.donga.com/politics.xml",
    ("it", "전자신문"): "https://rss.etnews.com/Section901.xml",
    ("it", "블로터"): "https://www.bloter.net/feed",
    ("politics", "BBC"): "https://feeds.bbci.co.uk/news/politics/rss.xml",
    ("it", "BBC"): "https://feeds.bbci.co.uk/news/technology/rss.xml",
    ("health", "BBC"): "https://feeds.bbci.co.uk/news/health/rss.xml",
    ("politics", "New York Times"): NYT.format("Politics"),
    ("it", "New York Times"): NYT.format("Technology"),
    ("health", "New York Times"): NYT.format("Health"),
    ("travel", "New York Times"): NYT.format("Travel"),
    ("fashion", "New York Times"): NYT.format("FashionandStyle"),
    ("lit", "New York Times"): NYT.format("Books"),
    ("politics", "Washington Post"): "https://feeds.washingtonpost.com/rss/politics",
    ("it", "Washington Post"): "https://feeds.washingtonpost.com/rss/business/technology",
}
KO_DAYS = "월화수목금토일"


def google_url(domain, kw, days, lang):
    q = f"site:{domain} ({kw}) when:{days}d" if kw else f"site:{domain} when:{days}d"
    p = ({"hl": "ko", "gl": "KR", "ceid": "KR:ko"} if lang == "ko"
         else {"hl": "en-US", "gl": "US", "ceid": "US:en"})
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, **p})


def fetch(url):
    try:
        return feedparser.parse(url, agent="Mozilla/5.0 (personal-news-app)").entries
    except Exception:  # noqa: BLE001
        return []


def translate(text):
    """영어 → 한국어 (구글 번역 공개 엔드포인트, 키 불필요). 실패하면 None."""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ko&dt=t&q="
           + urllib.parse.quote(text))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        return out or None
    except Exception:  # noqa: BLE001
        return None


def entry_time(e):
    p = e.get("published_parsed") or e.get("updated_parsed")
    if not p:
        return None
    return datetime.datetime(*p[:6], tzinfo=datetime.timezone.utc).astimezone(KST)


def collect(lang):
    outlets = KO_OUTLETS if lang == "ko" else EN_OUTLETS
    result, warnings = [], []
    for tid, ko, emoji, en, kw_ko, kw_en, days in TOPICS:
        kw = kw_ko if lang == "ko" else kw_en
        label = f"{emoji} {ko}"
        seen, items = set(), []
        for name, domain in outlets:
            if name in IT_ONLY and tid != "it":
                continue
            entries, fallback = [], False
            if (tid, name) in PRIMARY:
                entries = fetch(PRIMARY[(tid, name)])
            if not entries:
                fallback = True
                entries = fetch(google_url(domain, "" if name in IT_ONLY else kw, days, lang))
                time.sleep(0.5)
            if not entries:
                warnings.append(f"{label}/{name}")
                continue
            for e in entries[:PER_OUTLET]:
                link, title = e.get("link", ""), e.get("title", "").strip()
                if fallback:
                    title = re.sub(r"\s+[-–]\s+[^-–]+$", "", title)
                key = re.sub(r"\W+", "", title.lower())
                if not link or not title or link in seen or key in seen:
                    continue
                seen.update([link, key])
                items.append({"outlet": name, "title": title, "link": link, "t": entry_time(e)})
        items.sort(key=lambda x: x["t"] or datetime.datetime(1970, 1, 1, tzinfo=KST), reverse=True)
        if lang == "en":
            failed = 0
            for it in items:
                it["ko"] = translate(it["title"])
                failed += it["ko"] is None
                time.sleep(0.15)
            if failed:
                warnings.append(f"{label} 번역 실패 {failed}건(영어 원문으로 표시)")
        result.append((tid, label, items))
    return result, warnings


CSS = """
:root{--bg:#fafaf7;--fg:#1c1c1c;--sub:#777;--card:#fff;--line:#e6e4dc;--link:#0b57d0;--chip:#ece9df}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eee;--sub:#999;--card:#1e1e1e;--line:#2c2c2c;--link:#8ab4f8;--chip:#2a2a2a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif;line-height:1.5;
padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px 8px;z-index:5}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
h1{font-size:18px;margin:0}.upd{color:var(--sub);font-size:12px}
nav a{color:var(--link);font-size:13px;text-decoration:none;margin-left:12px}
.chips{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px;scrollbar-width:none}
.chips button{flex:none;border:0;background:var(--chip);color:var(--fg);padding:6px 12px;border-radius:999px;font-size:13px;cursor:pointer}
.chips button.on{background:var(--fg);color:var(--bg)}
main{max-width:720px;margin:auto;padding:8px 16px 48px}
section{margin-top:22px}h2{font-size:16px;margin:0 0 8px}
ul{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:12px}
li{padding:10px 14px;border-bottom:1px solid var(--line)}li:last-child{border:0}
li a{color:var(--fg);text-decoration:none;font-weight:500}li a:active{color:var(--link)}
.meta{color:var(--sub);font-size:12px;margin-top:2px}.empty{color:var(--sub);padding:12px 14px}
.warn{color:var(--sub);font-size:12px;margin-top:28px}
.orig{color:var(--sub);font-size:12.5px;margin-top:2px}
"""
JS = """
const chips=document.querySelectorAll('.chips button'),secs=document.querySelectorAll('section');
chips.forEach(b=>b.onclick=()=>{chips.forEach(c=>c.classList.remove('on'));b.classList.add('on');
secs.forEach(s=>s.style.display=(b.dataset.t==='all'||s.id===b.dataset.t)?'':'none');scrollTo(0,0)});
"""


def render(result, warnings, lang):
    now = datetime.datetime.now(KST)
    if lang == "ko":
        title, upd = "📰 내 뉴스", f"{now.month}/{now.day}({KO_DAYS[now.weekday()]}) {now:%H:%M} 갱신"
        nav, all_label, empty = '<a href="global.html">해외 뉴스</a>', "전체", "기사 없음"
        h_lang = "ko"
    else:
        title, upd = "🌐 해외 뉴스", f"{now.month}/{now.day}({KO_DAYS[now.weekday()]}) {now:%H:%M} 갱신 · 자동 번역"
        nav, all_label, empty = '<a href="index.html">국내 뉴스</a>', "전체", "기사 없음"
        h_lang = "ko"
    out = [f'<!doctype html><html lang="{h_lang}"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">',
           '<meta name="apple-mobile-web-app-capable" content="yes">',
           f'<meta name="apple-mobile-web-app-title" content="{html.escape(title[2:])}">',
           '<link rel="apple-touch-icon" href="icon.png"><link rel="icon" href="icon.png">',
           f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body>',
           f'<header><div class="top"><h1>{title}</h1><nav>{nav}</nav></div>',
           f'<div class="upd">{upd}</div><div class="chips"><button class="on" data-t="all">{all_label}</button>']
    out += [f'<button data-t="{tid}">{html.escape(label)}</button>' for tid, label, _ in result]
    out.append("</div></header><main>")
    for tid, label, items in result:
        out.append(f'<section id="{tid}"><h2>{html.escape(label)}</h2><ul>')
        if not items:
            out.append(f'<li class="empty">{empty}</li>')
        for it in items:
            tm = f" · {it['t']:%m/%d %H:%M}" if it["t"] else ""
            ko_title = it.get("ko")
            shown = ko_title or it["title"]
            orig = (f'<div class="orig" lang="en">{html.escape(it["title"])}</div>'
                    if ko_title else "")
            out.append(f'<li><a href="{html.escape(it["link"])}" target="_blank" rel="noopener">'
                       f'{html.escape(shown)}</a>{orig}'
                       f'<div class="meta">{html.escape(it["outlet"])}{tm}</div></li>')
        out.append("</ul></section>")
    if warnings:
        out.append(f'<p class="warn">⚠ {html.escape(", ".join(warnings))}</p>')
    out.append(f"</main><script>{JS}</script></body></html>")
    return "\n".join(out)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if os.path.exists("icon.png"):
        shutil.copy("icon.png", os.path.join(OUT, "icon.png"))
    for lang, fname in (("ko", "index.html"), ("en", "global.html")):
        data, warns = collect(lang)
        with open(os.path.join(OUT, fname), "w", encoding="utf-8") as f:
            f.write(render(data, warns, lang))
        print(fname, {label: len(items) for _, label, items in data}, "warn:", warns)

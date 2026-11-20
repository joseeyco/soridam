# -*- coding: utf-8 -*-
"""소담 사설 자동수집기 v3 — 무료 Bing News RSS 기반
- 신문사 16곳에서 각 1편, 최종 10편 목표
- 자수 필터: 700~1,000자(공백 제외)
- 본문 추출: JSON-LD articleBody → 섹터 셀렉터 → <p> 군집 3단 폴백
"""
import re, html as H, urllib.request, urllib.parse, ssl, json, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

OUTLETS = {  # 이름: (bing 쿼리용 도메인, 허용 호스트 접미사)
  "조선일보": ("chosun.com", "chosun.com"),
  "중앙일보": ("joongang.co.kr", "joongang.co.kr"),
  "동아일보": ("donga.com", "donga.com"),
  "한겨레":   ("hani.co.kr", "hani.co.kr"),
  "경향신문": ("khan.co.kr", "khan.co.kr"),
  "문화일보": ("munhwa.com", "munhwa.com"),
  "서울신문": ("seoul.co.kr", "seoul.co.kr"),
  "국민일보": ("kmib.co.kr", "kmib.co.kr"),
  "세계일보": ("segye.com", "segye.com"),
  "매일경제": ("mk.co.kr", "mk.co.kr"),
  "한국경제": ("hankyung.com", "hankyung.com"),
  "프레시안": ("pressian.com", "pressian.com"),
  "헤럴드경제": ("heraldcorp.com", "heraldcorp.com"),
  "뉴시스": ("newsis.com", "newsis.com"),
  "아시아경제": ("asiae.co.kr", "asiae.co.kr"),
  "이데일리": ("edaily.co.kr", "edaily.co.kr"),
}

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()

def bing_items(domain):
    for q in (f'"[사설]" site:{domain}', f'사설 site:{domain}', f'"[기고]" OR "논설" site:{domain}'):
        url = f"https://www.bing.com/news/search?q={urllib.parse.quote(q)}&format=RSS&setLang=ko"
        try:
            d = fetch(url).decode("utf-8", "replace")
        except Exception as e:
            print(f"  [bing err {domain}: {e}]", file=sys.stderr); continue
        items = []
        for it in re.findall(r"<item>(.*?)</item>", d, re.S):
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            l = re.search(r"<link>(.*?)</link>", it, re.S)
            if not (t and l): continue
            link = H.unescape(l.group(1))
            m = re.search(r"[?&]url=([^&]+)", link)
            real = urllib.parse.unquote(m.group(1)) if m else link
            items.append({"title": H.unescape(t.group(1)).strip(), "url": real})
        if items:
            return items
    return []

def decode_page(url):
    raw = fetch(url, 25)
    for enc in ("utf-8", "euc-kr", "cp949"):
        try: return raw.decode(enc)
        except UnicodeDecodeError: continue
    return raw.decode("utf-8", "replace")

def jsonld_body(d):
    for m in re.finditer(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', d):
        try:
            txt = json.loads('"' + m.group(1) + '"')
            if len(re.sub(r"\s", "", txt)) > 300:
                return txt
        except Exception:
            continue
    return None

BODY_SELECTORS = {
  "chosun.com":  [r'<div[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>',
                  r'<section[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</section>'],
  "hani.co.kr":  [r'<div[^>]*class="[^"]*article-text[^"]*"[^>]*>(.*?)</div>',
                  r'<div[^>]*class="[^"]*text[^"]*"[^>]*>(.*?)</div>'],
  "donga.com":   [r'<div[^>]*id="article_txt"[^>]*>(.*?)</div>'],
  "khan.co.kr":  [r'<div[^>]*class="[^"]*(?:art_body|article-body)[^"]*"[^>]*>(.*?)</div>',
                  r'<article[^>]*class="[^"]*art_body[^"]*"[^>]*>(.*?)</article>'],
  "munhwa.com":  [r'<div[^>]*id="news_body_area"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articlebody"[^>]*>(.*?)</div>'],
  "seoul.co.kr": [r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>',
                  r'<div[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>'],
  "kmib.co.kr":  [r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articlebody"[^>]*>(.*?)</div>'],
  "segye.com":   [r'<div[^>]*id="article_body"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>'],
  "mk.co.kr":    [r'<div[^>]*class="[^"]*(?:art_body|plain_txt|txt_article)[^"]*"[^>]*>(.*?)</div>'],
  "hankyung.com":[r'<div[^>]*id="news-view"[^>]*>(.*?)</div>',
                  r'<div[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>'],
  "joongang.co.kr":[r'<div[^>]*(?:id|class)="[^"]*(?:article_body|article-body|ab_paragraph)[^"]*"[^>]*>(.*?)</div>'],
  "pressian.com":[r'<div[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>'],
  "heraldcorp.com":[r'<div[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>'],
  "newsis.com":  [r'<div[^>]*class="[^"]*par[gc]?[^"]*"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>'],
  "asiae.co.kr": [r'<div[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>'],
  "edaily.co.kr":[r'<div[^>]*class="[^"]*news_body[^"]*"[^>]*>(.*?)</div>',
                  r'<div[^>]*id="articleBody"[^>]*>(.*?)</div>'],
}
NOISE = re.compile(r"(www\.|https?://\S+|ⓒ|©|Copyright|무단\s*전재|재배포\s*금지|이메일\s*:|.*@.*\.)", re.I)

def clean_text(body_html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body_html, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|div)>", "\n", text)
    text = H.unescape(re.sub(r"<[^>]+>", "", text))
    lines = []
    for ln in (l.strip() for l in text.split("\n")):
        if ln and not NOISE.search(ln) and not ln.startswith(("▲","▶","※","◆","■","→","-")):
            if len(ln) > 1 and not re.fullmatch(r"[\s\d\W]*", ln):
                lines.append(ln)
    return "\n".join(lines).strip()

def extract_text(url, base_domain):
    try:
        if not re.search(rf"https?://(www\.)?([a-z0-9-]+\.)*{re.escape(base_domain)}(/|$)", url):
            return None  # 자매/자식 도메인 제외 (kids.donga.com 등)
        d = decode_page(url)
        # 1) JSON-LD
        jb = jsonld_body(d)
        if jb:
            txt = clean_text(jb.replace("\\n", "\n"))
            if len(re.sub(r"\s", "", txt)) > 300:
                return txt
        # 2) 섹터 셀렉터
        for sel in BODY_SELECTORS.get(base_domain, []):
            m = re.search(sel, d, re.S)
            if m:
                txt = clean_text(m.group(1))
                if len(re.sub(r"\s", "", txt)) > 300:
                    return txt
        # 3) <p> 군집
        paras = [p for p in re.findall(r"<p[^>]*>(.*?)</p>", d, re.S)
                 if len(re.sub(r"<[^>]+>|\s", "", p)) > 30]
        if paras:
            txt = clean_text("\n".join(paras))
            if len(re.sub(r"\s", "", txt)) > 300:
                return txt
        return None
    except Exception:
        return None

def collect_outlet(name, domain):
    if isinstance(domain, tuple):
        domain = domain[0]
    items = bing_items(domain)
    in_range, backup = None, None
    items = [c for c in items if "사설" in c["title"]]
    for c in items:
        txt = extract_text(c["url"], domain)
        if not txt: continue
        nosp = len(re.sub(r"\s", "", txt))
        c.update(outlet=name, chars_nospace=nosp, text=txt)
        if 700 <= nosp <= 1000:
            in_range = c; break
        if backup is None or abs(nosp - 850) < abs(backup["chars_nospace"] - 850):
            backup = c
    return name, in_range, backup

def main():
    print(f"=== 소담 사설 수집 {TODAY.strftime('%Y-%m-%d %H:%M')} KST ===")
    import time
    res = {}
    for i, kv in enumerate(OUTLETS.items()):
        res[kv[0]] = collect_outlet(*kv)
        time.sleep(1.2)  # 빙 순차 요청 (차단 방지)
    in_range = [v[1] for v in res.values() if v[1]]
    backups  = [v[2] for v in res.values() if v[2] and not v[1] and "사설" in v[2]["title"]]
    backups.sort(key=lambda x: abs(x["chars_nospace"] - 850))
    in_range.sort(key=lambda x: abs(x["chars_nospace"] - 850))
    final = in_range[:10]
    if len(final) < 10:
        final += backups[:10 - len(final)]
    import os
    os.makedirs("data", exist_ok=True)
    out_path = f"data/{TODAY.strftime('%Y-%m-%d')}.json"
    out = {"date": TODAY.strftime("%Y-%m-%d"),
           "criteria": "700~1,000자(공백 제외), 신문사당 1편, 목표 10편",
           "picked": final,
           "summary": [{"outlet": p["outlet"], "title": p["title"], "chars": p["chars_nospace"],
                        "in_range": 700 <= p["chars_nospace"] <= 1000, "url": p["url"]} for p in final]}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for p in final:
        flag = "✅" if 700 <= p["chars_nospace"] <= 1000 else "↗"
        print(f'{flag} [{p["outlet"]}] {p["chars_nospace"]}자 | {p["title"][:55]}')
        print(f'      {p["url"][:110]}')
    print(f"\n범위내 {len(in_range)}편 / 백업 {len(backups)}편 → 최종 {len(final)}편")

if __name__ == "__main__":
    main()

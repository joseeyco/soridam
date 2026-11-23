# -*- coding: utf-8 -*-
"""소담 사설 자동수집기 v3 — 무료 Bing News RSS 기반
- 신문사 16곳에서 각 1편, 최종 10편 목표
- 자수 필터: 700~1,000자(공백 제외)
- 본문 추출: JSON-LD articleBody → 섹터 셀렉터 → <p> 군집 3단 폴백
"""
import re, html as H, urllib.request, urllib.parse, ssl, json, sys, glob, os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

# 글자수 규격: 1,100자 절대 상한 — 배분은 600~700×4 · 800~900×4 · 950~1,100×2
BUCKETS = [("600~700자", 600, 700, 4, 650),
           ("800~900자", 800, 900, 4, 850),
           ("1,000자",   950, 1100, 2, 1000)]
CHAR_MIN, CHAR_MAX = 600, 1100

def clean_title(t):
    """[사설]·[기고] 등 괄호 문구 제거"""
    t = re.sub(r"^\s*[\[【(]\s*(사설|기고|논설|칼럼|왜|단상|시평)\s*[\]】)]\s*", "", str(t or ""))
    t = re.sub(r"\s*[\[【]\s*사설\s*[\]】]\s*$", "", t.strip())
    return t.strip()

def keywords(title):
    """주제 겹침 판단용: 제목의 핵심 어구 집합"""
    t = re.sub(r"[\[【\]】()“”\"'·…─\-]", " ", str(title))
    return set(w for w in re.split(r"\s+", t) if len(w) >= 2)

def topic_ok(title, chosen_titles):
    """핵심 어구가 기선택 사설과 2개 이상 겹치면 같은 주제로 간주"""
    kw = keywords(title)
    for t2 in chosen_titles:
        if len(kw & keywords(t2)) >= 2:
            return False
    return True

def trim_to_chars(text, lo=800, hi=950):
    """글자수 초과 사설을 문장 경계에서 800~950자(공백 제외)로 정리"""
    t = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    sents = [s for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
    nosp = 0; keep = []
    for s in sents:
        n = len(re.sub(r"\s", "", s))
        if nosp + n <= hi:
            keep.append(s); nosp += n
        if nosp >= lo:
            break
    if keep and lo <= len(re.sub(r"\s", "", " ".join(keep))):
        return " ".join(keep)
    # 문장 경계로 800자에 못 미치면 문자 단위 절단(마지막 문장부호)
    cut = t[:hi*4]; acc = 0; out = -1
    for i, ch in enumerate(cut):
        if not re.match(r"\s", ch): acc += 1
        if acc >= lo and ch in ".!?": out = i; break
        if acc > hi: out = i; break
    return (cut[:out+1] if out > 0 else cut).strip()

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

def collect_outlet(name, domain, max_cands=3):
    """신문사당 후보 여러 편(600~1,100자) — 헤더 제목은 [신문사] 형태"""
    if isinstance(domain, tuple):
        domain = domain[0]
    items = bing_items(domain)
    items = [c for c in items if "사설" in c["title"]]
    cands, seen_local = [], set()
    for c in items:
        ct = f"[{name}] " + clean_title(c["title"])
        if ct in seen_local: continue
        txt = extract_text(c["url"], domain)
        if not txt: continue
        nosp = len(re.sub(r"\s", "", txt))
        if not (CHAR_MIN <= nosp <= CHAR_MAX): continue
        seen_local.add(ct)
        c.update(title=ct, outlet=name, chars_nospace=nosp, text=txt)
        cands.append(c)
        if len(cands) >= max_cands: break
    return name, cands

def load_prev_titles():
    prev = set()
    for f in sorted(glob.glob("data/*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                for p in json.load(fh).get("picked", []):
                    prev.add(p.get("title", ""))
        except Exception:
            continue
    return prev

def alloc(pool, prev_titles, relax=False):
    """배분: 600~700×4 · 800~900×4 · 950~1,100×2 + 주제 겹침 방지
    relax=True: 글자수 범위 ±50 확장 + 신문사 중복 허용"""
    lo_all, hi_all = (550, 1150) if relax else (CHAR_MIN, CHAR_MAX)
    final, used_outlets, used_titles, chosen = [], set(), set(prev_titles), []
    for _, lo, hi, want, center in (BUCKETS if not relax else
        [(b[0], max(b[1]-50,550), min(b[2]+50,1150), b[3], b[4]) for b in BUCKETS]):
        cands = [c for c in pool if lo <= c["chars_nospace"] <= hi
                 and c["title"] not in used_titles
                 and (relax or c["outlet"] not in used_outlets)
                 and topic_ok(c["title"], [f["title"] for f in chosen])]
        cands.sort(key=lambda c: abs(c["chars_nospace"] - center))
        for c in cands[:want]:
            used_outlets.add(c["outlet"]); used_titles.add(c["title"]); final.append(c); chosen.append(c)
    if len(final) < 10:
        rest = [c for c in pool if c["title"] not in used_titles
                and (relax or c["outlet"] not in used_outlets)
                and lo_all <= c["chars_nospace"] <= hi_all
                and topic_ok(c["title"], [f["title"] for f in chosen])]
        rest.sort(key=lambda c: abs(c["chars_nospace"] - 780))
        for c in rest[:10 - len(final)]:
            used_outlets.add(c["outlet"]); used_titles.add(c["title"]); final.append(c); chosen.append(c)
    return final

def top_up(need, excluded_titles):
    """폴백: 후보 상한 6편으로 재수집"""
    extra = []
    for name, kv in OUTLETS.items():
        if len(extra) >= need * 3: break
        _, cands = collect_outlet(name, kv, max_cands=6)
        extra.extend(c for c in cands if c["title"] not in excluded_titles)
    return extra

def main():
    print(f"=== 소담 사설 수집 {TODAY.strftime('%Y-%m-%d %H:%M')} KST ===")
    import time
    prev_titles = load_prev_titles()
    pool, dup = [], 0
    for kv in OUTLETS.items():
        _, cands = collect_outlet(*kv)
        dup += len([c for c in cands if c["title"] in prev_titles])
        pool.extend(cands)
        time.sleep(1.2)
    pool = [c for c in pool if c["title"] not in prev_titles]
    final = alloc(pool, prev_titles)
    if len(final) < 10:
        print(f"1차 {len(final)}편 — 폴백 1단계: 후보 확대 + 신문사 중복 허용", file=sys.stderr)
        excl = set(prev_titles) | {c["title"] for c in final}
        pool2 = pool + top_up(10 - len(final), excl)
        final = alloc(pool2, prev_titles, relax=True)
    if len(final) < 10:
        print(f"2차 {len(final)}편 — 폴백 2단계: 500~1,200자 확장 채움", file=sys.stderr)
        rest = [c for c in pool if 500 <= c["chars_nospace"] <= 1200
                and c["title"] not in {f["title"] for f in final}]
        rest.sort(key=lambda c: abs(c["chars_nospace"] - 780))
        final += rest[:10 - len(final)]
    # 1,100자 절대 상한 — 초과분은 800~950자로 문장 정리
    for c in final:
        if c["chars_nospace"] > 1100:
            c["text"] = trim_to_chars(c["text"])
            c["chars_nospace"] = len(re.sub(r"\s", "", c["text"]))
            c["trimmed"] = True
    final.sort(key=lambda c: abs(c["chars_nospace"] - 780))
    os.makedirs("data", exist_ok=True)
    out_path = f"data/{TODAY.strftime('%Y-%m-%d')}.json"
    out = {"date": TODAY.strftime("%Y-%m-%d"),
           "criteria": "600~700자 4편 · 800~900자 4편 · 1,000자 2편(최대 1,100자 상한), 주제 비겹침, 신문사당 1편, 전일 중복 제외",
           "picked": final,
           "summary": [{"outlet": p["outlet"], "title": p["title"], "chars": p["chars_nospace"],
                        "bucket": next((b[0] for b in BUCKETS if b[1] <= p["chars_nospace"] <= b[2]), "외"),
                        "trimmed": p.get("trimmed", False), "url": p["url"]} for p in final]}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for p in final:
        bucket = next((b[0] for b in BUCKETS if b[1] <= p["chars_nospace"] <= b[2]), "범위밖")
        mark = "✂" if p.get("trimmed") else "✅"
        print(f'{mark} [{p["outlet"]}] {p["chars_nospace"]}자 ({bucket}) | {p["title"][:55]}')
        print(f'      {p["url"][:110]}')
    print(f"\n후보 {len(pool)}편(전일 중복 {dup}편 제외) → 최종 {len(final)}편")

if __name__ == "__main__":
    main()

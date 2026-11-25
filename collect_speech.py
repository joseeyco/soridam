# -*- coding: utf-8 -*-
"""연설체 수집기 v2.1 — 대통령실 연설문 자료실(president.go.kr/speeches) 기반
기준: 속기 기출 스타일 · 최신 대통령 연설 전문 · 700~1,090자(공백 제외) · 일 1편 · 과거 수집분 중복 금지
목록은 사이트맵(/sitemap.xml)과 raw 페이지에서, 본문은 상세 페이지(서버렌더)에서 추출"""
import os, re, json, sys, html as H
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import collect_editorials as ce

BASE="https://www.president.go.kr/speeches"
OUTDIR="data/speech"

def strip_tags(s):
    s=re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S|re.I)
    s=re.sub(r"<[^>]+>", "\n", s)
    s=H.unescape(s)
    return re.sub(r"[ \t]+", " ", s)

def candidate_urls():
    seen=[]
    def add(u):
        if u not in seen: seen.append(u)
    try:
        d=ce.fetch(BASE,30).decode('utf-8','replace')
        for m in re.finditer(r'href="(/speeches/[A-Za-z0-9]+)"', d): add("https://www.president.go.kr"+m.group(1))
    except Exception as e: print("list err:", e, file=sys.stderr)
    try:
        d=ce.fetch("https://www.president.go.kr/sitemap.xml",30).decode('utf-8','replace')
        for m in re.finditer(r"<loc>\s*(https://www\.president\.go\.kr/speeches/[A-Za-z0-9]+)\s*</loc>", d): add(m.group(1))
    except Exception as e: print("sitemap err:", e, file=sys.stderr)
    return seen

def get_detail(url):
    d=ce.fetch(url,30).decode('utf-8','replace')
    t=re.search(r"<title>(.*?)</title>", d, re.S)
    title=H.unescape(t.group(1)).strip() if t else ""
    title=re.sub(r"\s*[-|]\s*(대한민국 대통령실|대통령실).*$","",title).strip()
    txt=strip_tags(d)
    lines=[p.strip() for p in txt.split("\n")]
    # 연설 본문 블록: 한글 포함 5자 이상 문단만 모아 재결합
    paras=[p for p in lines if len(re.findall(r'[가-힣]', p))>=5]
    body="\n".join(paras)
    # 상단 내비/푸터 노이즈 절제
    body=re.sub(r"(?m)^(대통령실|대통령 서훈|모두발언|바로가기|본문 바로가기|메뉴|검색|로그인|이전|다음|목록|인쇄|공유|SNS 공유|TOP|취소|확인)\s*$", "\n", body)
    return title, body

def prev_seen():
    import glob as g
    prev=set()
    for f in g.glob(OUTDIR+'/*.json'):
        try:
            j=json.load(open(f,encoding='utf-8'))
            for pp in j.get('picked',[]):
                prev.add(pp.get('url','')); prev.add(ce.title_key(pp.get('title','')))
        except Exception: pass
    return prev

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    prev=prev_seen()
    urls=candidate_urls()
    print(f"후보 URL {len(urls)}개", flush=True)
    for url in urls:
        if url in prev: continue
        try: title, body=get_detail(url)
        except Exception as e:
            print("detail err:", e, file=sys.stderr); continue
        fitted=ce.fit_length(body)
        n=ce.nospace(fitted)
        if n<700:
            print(f"짧음({n}자): {url}"); continue
        title=title or fitted.split("\n")[0][:40]
        today=ce.TODAY.strftime('%Y-%m-%d')
        entry={'no':1,'date':today,'title':f"[대통령실] {ce.clean_title(title)}",'src':'대통령실 연설문 자료실',
               'chars':n,'orig_chars':n,'excerpted':n>1090,'relaxed':False,
               'lv':1 if n<=850 else (2 if n<=1000 else 3),'pub':today,'url':url,'body':fitted,'type':'speech'}
        out={'date':today,'type':'speech','criteria':'대통령실 연설문 자료실 · 최신 연설 전문 · 700~1,090자(공백 제외) · 일 1편 · 과거 수집분 중복 금지','picked':[entry]}
        json.dump(out, open(f"{OUTDIR}/{today}.json",'w',encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f"speech OK: [{ce.clean_title(title)[:40]}] ({n}자) {url}", flush=True)
        return 0
    print("speech FAIL: 새 후보 없음", file=sys.stderr, flush=True)
    return 1

sys.exit(main())

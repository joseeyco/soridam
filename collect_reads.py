# -*- coding: utf-8 -*-
"""낭독 스크립트 수집기 v1 — 연설체(speech) / 논설체(column) 매일 1편씩
기준: 속기 기출 스타일 · 최신 연설/논설문 · 700~1,090자(공백 제외) · 일 1편 · 과거 수집분 중복 금지
논설체: 각 신문사의 [기고]/[칼럼]/[논설] (Bing News RSS)
연설체: 연설·축사·기자회견 '전문' 게시 기사 본문"""
import os, re, json, sys, time, glob as g
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import collect_editorials as ce
from email.utils import parsedate_to_datetime
from datetime import timedelta
import urllib.parse, html as H

OUT={'speech':'data/speech','column':'data/column'}

def bing_pool(queries, tfilter, max_age_h=24*7):
    seen,out=set(),[]
    for q in queries:
        url=f"https://www.bing.com/news/search?q={urllib.parse.quote(q)}&format=RSS&setLang=ko"
        try: d=ce.fetch(url).decode('utf-8','replace')
        except Exception as e: print(f'  bing err: {e}', file=sys.stderr); continue
        d=re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", d, flags=re.S)
        for it in re.findall(r"<item>(.*?)</item>", d, re.S):
            t=re.search(r"<title>(.*?)</title>", it, re.S); l=re.search(r"<link>(.*?)</link>", it, re.S); pp=re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
            if not (t and l): continue
            title=H.unescape(t.group(1)).strip()
            if not tfilter(title): continue
            link=H.unescape(l.group(1)).strip()
            m=re.search(r"[?&]url=([^&]+)", link); real=urllib.parse.unquote(m.group(1)) if m else link
            if real in seen: continue
            pub=None
            if pp:
                try:
                    pub=parsedate_to_datetime(pp.group(1).strip())
                    if pub.tzinfo is None: pub=pub.replace(tzinfo=ce.timezone.utc)
                    pub=pub.astimezone(ce.KST)
                except Exception: pub=None
            if pub and ce.TODAY-pub>timedelta(hours=max_age_h): continue
            seen.add(real); out.append({'title':title,'url':real,'pub':pub.isoformat() if pub else None})
        if len(out)>=12: break
        time.sleep(0.5)
    return out

def is_column(t): return bool(re.search(r"\[(기고|칼럼|논설|시론)\]|[【(](기고|칼럼|논설)", t)) or bool(re.search(r"(기고|칼럼|논설)\s*[·:]", t))
def is_speech(t): return bool(re.search(r"(연설|축사|석학|기자회견)[^가-힣]{0,6}전문|전문[^가-힣]{0,6}(연설|축사)", t))

def prev_seen(outdir):
    prev=set()
    for f in g.glob(outdir+'/*.json'):
        try:
            j=json.load(open(f,encoding='utf-8'))
            for pp in j.get('picked',[]):
                prev.add(ce.title_key(pp.get('title',''))); prev.add(ce.norm_url(pp.get('url','')))
        except Exception: pass
    return prev

def pick_one(kind, queries, tfilter, min_chars=700):
    outdir=OUT[kind]; os.makedirs(outdir, exist_ok=True)
    prev=prev_seen(outdir)
    for name,dom in ce.OUTLETS.items():
        for c in bing_pool([q.format(d=dom) for q in queries], tfilter):
            if ce.norm_url(c['url']) in prev or ce.title_key(c['title']) in prev: continue
            txt=ce.extract_text(c['url'], dom)
            if not txt: continue
            txt=ce.strip_annotations(txt)
            orig=ce.nospace(txt)
            if orig<min_chars: continue
            fitted,exc=(ce.fit_length(txt) if orig>=1090 else (txt,False))
            if not fitted: continue
            n=ce.nospace(fitted)
            entry={'no':1,'date':ce.TODAY.strftime('%Y-%m-%d'),'title':ce.clean_title(c['title']),
                   'src':name,'chars':n,'orig_chars':orig,'excerpted':exc,'relaxed':False,
                   'lv':1 if n<=850 else (2 if n<=1000 else 3),'pub':c['pub'],'url':c['url'],'body':fitted,'type':kind}
            out={'date':entry['date'],'type':kind,'criteria':'속기 기출 스타일 · 최신 연설/논설문 · 700~1,090자(공백 제외) · 일 1편 · 과거 수집분 중복 금지','picked':[entry]}
            json.dump(out, open(f"{outdir}/{entry['date']}.json",'w',encoding='utf-8'), ensure_ascii=False, indent=1)
            print(f"{kind} OK: [{name}] {entry['title'][:44]} ({n}자)", flush=True)
            return True
        time.sleep(0.6)
    print(f"{kind} FAIL: 기준에 맞는 후보 없음", file=sys.stderr, flush=True)
    return False

ok_c=pick_one('column', ['"[기고]" site:{d}','"[칼럼]" site:{d}','"[논설]" site:{d}'], is_column)
ok_s=pick_one('speech', ['연설 전문 site:{d}','축사 전문 site:{d}','기자회견 전문 site:{d}','연설문 site:{d}'], is_speech)
sys.exit(0 if ok_c else 1)  # 연설체는 collect_speech.py가 담당

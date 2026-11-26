# -*- coding: utf-8 -*-
"""수집 데이터 누적 넘버링 — 유형별(사설/연설체/논설체)로 과거 첫 글을 1번으로 매긴다.
날짜 오름차순(같은 날은 파일 내 기존 순서)으로 정렬해 no를 다시 매기고 파일에 되돌려 쓴다."""
import json, glob, os

for label, d in [('editorial(사설)','data'), ('speech(연설체)','data/speech'), ('column(논설체)','data/column')]:
    files = sorted(glob.glob(os.path.join(d,'*.json')))
    entries=[]
    for f in files:
        try: j=json.load(open(f,encoding='utf-8'))
        except Exception:
            print(f"  읽기 실패: {f}"); continue
        pk=j.get('picked',[]) if isinstance(j,dict) else []
        for p in pk: p['_f']=f
        entries.extend(pk)
    entries.sort(key=lambda p: str(p.get('date','')))   # 안정 정렬: 같은 날은 기존 순서 유지
    for i,p in enumerate(entries,1): p['no']=i
    byfile={}
    for p in entries: byfile.setdefault(p.pop('_f'),[]).append(p)
    for f, pk in byfile.items():
        try: j=json.load(open(f,encoding='utf-8'))
        except Exception: continue
        j['picked']=pk
        json.dump(j, open(f,'w',encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"{label}: {len(entries)}편 누적 리넘버링 완료 ({len(files)}개 파일)")

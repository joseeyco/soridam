# -*- coding: utf-8 -*-
"""소담 사설 자동수집기 v5 — 무료 Bing News RSS 기반 (GitHub Actions 매일 04:00 KST 실행용)

v5 변경점 (v4 대비)
 1) 700 ~ 1,099자(공백 제외)  : 1,100자 이상이면 버리지 않고 '문장 단위 자동발췌'로 맞춤
 2) 과거 수집분 제외          : data/*.json 의 URL · 제목 · 본문 앞부분으로 중복 판정
 3) 같은 날 동일 주제 회피    : 제목/본문 키워드 유사도로 판정 (휴리스틱)
 4) 동일 신문사 1편           : 신문사당 최대 1편
 5) 본문 정제                 : 광고/인쇄/공유/스크랩/기자 바이라인/사진 캡션 등 제거
                               (중첩 <div> 때문에 본문이 잘리던 정규식 방식을 HTML 파서로 교체)
 6) 최신 기사만               : Bing pubDate 기준 48시간 이내
 7) 신문사 순서 편향 제거     : 날짜 시드로 신문사 순서를 섞어서 선택
 8) 괄호 제거 범위 축소       : 영문·한자가 들어있는 괄호만 제거 ((1.5%) 같은 숫자 괄호는 유지)
 9) SSL 인증서 검증 켬, 네트워크 재시도 추가
10) 기사 아닌 조각 제거   : 내비게이션·머리기사 목록·요약문구·브랜드 한글 표기
                               ('구글에서 서울신문 먼저 보기', '#대출규제 풍선 효과',
                                '엠에이이아이엘 비유에스…', '2026년 11월 28일 (토)' 등)
유지: [신문사] 제목 헤더 · 길이 오름차순 1~10번
"""
import os, re, json, sys, ssl, time, random, html as H, unicodedata
import urllib.request, urllib.parse
from collections import Counter
from glob import glob
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()

MIN_CHARS = 700          # 공백 제외 최소
MAX_EXCL = 1100          # 1,100 '미만'만 허용
EXCERPT_CAP = 1090       # 발췌 시 목표 상한
TARGET_CHARS = 900       # 후보 정렬 기준점
PICK = 10
MAX_AGE_H = 48           # 이 시간보다 오래된 기사는 제외
PER_OUTLET_TRY = 4       # 신문사당 확인할 후보 수
DATA_DIR = "data"

OUTLETS = {  # 이름: bing 쿼리/허용 도메인
    "조선일보": "chosun.com",
    "중앙일보": "joongang.co.kr",
    "동아일보": "donga.com",
    "한겨레": "hani.co.kr",
    "경향신문": "khan.co.kr",
    "문화일보": "munhwa.com",
    "서울신문": "seoul.co.kr",
    "국민일보": "kmib.co.kr",
    "세계일보": "segye.com",
    "매일경제": "mk.co.kr",
    "한국경제": "hankyung.com",
    "프레시안": "pressian.com",
    "헤럴드경제": "heraldcorp.com",
    "뉴시스": "newsis.com",
    "아시아경제": "asiae.co.kr",
    "이데일리": "edaily.co.kr",
}


def nospace(s):
    return len(re.sub(r"\s", "", s or ""))


def fetch(url, timeout=20, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


# ─────────────────────────────────────────────────────────────
# 괄호 안 영문·한자 첨자 제거 (한글이 있거나 숫자뿐인 괄호는 유지)
# ─────────────────────────────────────────────────────────────
HAN = re.compile(r"[가-힣]")
LATIN_HANJA = re.compile(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff]")
PAREN_GROUP = re.compile(r"[(（]\s*([^()（）]{0,40}?)\s*[)）]")


def _paren_repl(m):
    inner = m.group(1)
    if HAN.search(inner):
        return m.group(0)
    return "" if LATIN_HANJA.search(inner) else m.group(0)


def _drop_unbalanced(s):
    stack, drop = [], set()
    for i, ch in enumerate(s):
        if ch in "(（":
            stack.append(i)
        elif ch in ")）":
            if stack:
                stack.pop()
            else:
                drop.add(i)
    drop.update(stack)
    return "".join(c for i, c in enumerate(s) if i not in drop)


def strip_annotations(text):
    if not text:
        return text
    s = text
    for _ in range(4):
        before = s
        s = PAREN_GROUP.sub(_paren_repl, s)
        if s == before:
            break
    s = _drop_unbalanced(s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"[ \t]+([,.;:!?])", r"\1", s)
    return s.strip()


# ─────────────────────────────────────────────────────────────
# 제목 정리 + [신문사] 헤더
# ─────────────────────────────────────────────────────────────
TITLE_PREFIX = re.compile(r"^\s*[\[\(（【]\s*(?:사설|기고|논설|오피니언|칼럼|시론|특집|분석)\s*[\]\)）】]\s*")
TITLE_OUTLET = re.compile(r"^\s*\[[^\]]{1,10}\]\s*")
TITLE_TAIL = re.compile(r"\s+[-–—|]\s+([^-–—|]{1,20})$")      # Bing 제목 뒤 " - 조선일보"


def _strip_tail(t):
    m = TITLE_TAIL.search(t)
    if m:
        tail = m.group(1)
        if any(n in tail for n in OUTLETS) or re.search(r"\.(com|kr|net)$", tail.strip()):
            return t[:m.start()]
    return t


def clean_title(t):
    s = H.unescape(t or "").strip()
    s = _strip_tail(s)
    for _ in range(3):
        before = s
        s = TITLE_PREFIX.sub("", s)
        s = TITLE_OUTLET.sub("", s)
        if s == before:
            break
    s = strip_annotations(s)
    s = normalize_text(s)
    s = re.sub(r"^[\s\-–—:·|]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


try:
    import hanja as _hanja
except ImportError:
    _hanja = None

def hanja_to_ko(t):
    """한자(이체자 포함) → 한글 (예: 兆→조, 勞→노, 李→이, 展開→전개). 라이브러리 미설치 시 한자만 제거."""
    t = str(t)
    if _hanja:
        t = unicodedata.normalize("NFKC", t)
    if not _hanja:
        return re.sub(r"[\u3400-\u9FFF\uF900-\uFAFF]", " ", t)
    try:
        return _hanja.translate(str(t), "substitution")
    except Exception:
        return re.sub(r"[\u3400-\u9FFF\uF900-\uFAFF]", " ", t)

LATIN2KO = {"a":"에이","b":"비","c":"씨","d":"디","e":"이","f":"에프","g":"지","h":"에이치","i":"아이","j":"제이","k":"케이","l":"엘","m":"엠","n":"엔","o":"오","p":"피","q":"큐","r":"알","s":"에스","t":"티","u":"유","v":"브이","w":"더블유","x":"엑스","y":"와이","z":"제이"}
LATIN_RE = re.compile(r"[A-Za-z]")
SYMBOL_RE = re.compile(r"[\u2018\u2019\u201c\u201d\u3008\u3009\u300c\u300d\u300e\u300f\u00ab\u00bb\u2039\u203a<>\"'`\u00b7\u2025\u2026\u2013\u2014\u2010\u2011\u2212\uff0b\u2260\u2264\u2265\u2192\u2190\u2191\u2193\u00a9\u00ae\u2113\u203b\u25c6\u25cf\u25aa\u25a1\u25cb\u25b3\u2211\u00b1\u00d7]")

def normalize_text(t):
    """한자→한글 · 기호 최소화 · 영문→한글 음차 (예: 兆단위→조단위, k→케이, %→퍼센트)"""
    t = str(t)
    t = hanja_to_ko(t)
    t = SYMBOL_RE.sub(" ", t)
    t = t.replace("%", "퍼센트")
    t = LATIN_RE.sub(lambda m: LATIN2KO[m.group(0).lower()], t)
    t = re.sub(r"\s{2,}", " ", t)
    return t

# ─────────────────────────────────────────────────────────────
# 문장·띄어쓰기 재검토(교정) — 띄어쓰기 오류 검증 + 맥락에 맞지 않는 조각 정리
# ─────────────────────────────────────────────────────────────
JOSA_ATTACH = ("은", "는", "이", "가", "을", "를", "의", "에", "에서", "에게", "께서", "도", "만",
               "와", "과", "로", "으로", "처럼", "보다", "까지", "부터", "밖에", "마저", "조차",
               "이며", "이고", "이란", "라는", "라고", "이다", "입니다", "였다", "한다", "했다",
               "된다", "됐다", "하는", "한", "할", "해서", "하면", "해도", "하지", "인", "적인")
PARTICLE_HEAD = re.compile(r"^(은|는|이|가|을|를|의|에|에서|와|과|로|으로|도|만|며|고|나|면|라고|라는)\s")
UNIT_ATTACH = re.compile(r"(\d)\s+(명|개|곳|건|원|년|월|일|시|분|초|위|배|퍼센트|조|억|만|천|시간|개국|포인트|쪽|차|회|번|살|세|층|호|개사)")
SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?%)\]】」』”’])")
SPACE_AFTER_OPEN = re.compile(r"([(\[【「『“‘])\s+")
DUP_PUNCT = re.compile(r"([,.!?])\1+")
BAD_TAIL = re.compile(r"(무단\s*전재|재배포\s*금지|ⓒ|Copyright|저작권자)", re.I)


def polish_spacing(t):
    """띄어쓰기 교정: 여는 괄호·따옴표 뒤 공백, 문장부호 앞 공백, 숫자+단위, 조사·어미 붙여쓰기."""
    t = SPACE_AFTER_OPEN.sub(r"\1", t)
    t = SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = UNIT_ATTACH.sub(r"\1\2", t)
    t = DUP_PUNCT.sub(r"\1", t)
    for j in JOSA_ATTACH:                     # '정부 가' → '정부가' 처럼 조사는 앞말에 붙인다
        t = re.sub(rf"([가-힣])\s+({j})(?=[\s,.!?\"'”’)\]]|$)", r"\1\2", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def polish_text(t):
    """수집 원문 재검토: 잘린 조각 병합 → 중복·잡음 문장 제거 → 문장부호 정리 → 띄어쓰기 교정."""
    if not t:
        return t
    t = t.replace("\\엔", " ").replace("\u200b", " ")
    sents = []
    for para in t.split("\n"):
        para = para.strip()
        if not para:
            continue
        for x in SENT_SPLIT.split(para):
            x = x.strip()
            if x:
                sents.append(x)
    merged = []
    for x in sents:
        if BAD_TAIL.search(x):
            continue
        # 앞 문장이 끝나지 않았는데 조사·연결어미로 시작하면 잘린 조각 → 앞 문장에 붙인다
        if merged and (PARTICLE_HEAD.match(x) or re.match(r"^[a-z(]", x)) and not re.search(r"[.!?…]$", merged[-1]):
            merged[-1] = merged[-1].rstrip() + " " + x
        else:
            merged.append(x)
    out = []
    for x in merged:
        x = re.sub(r"\s{2,}", " ", x).strip()
        if len(re.sub(r"\s", "", x)) < 8 and not re.search(r"[.!?…]$", x):
            continue                          # 캡션·바이라인 잔여 조각 제거
        if x in out:
            continue                          # 같은 문장이 반복되면 한 번만 남긴다
        out.append(x)
    fixed = []
    for x in out:
        if not re.search(r"[.!?…\"'”’)\]]$", x):
            x += "."
        fixed.append(x)
    body = polish_spacing(" ".join(fixed))
    return re.sub(r"\s{2,}", " ", body).strip()


def with_outlet(outlet, title):
    return f"[{outlet}] {clean_title(title)}"


EDITORIAL_TAG = re.compile(r"[\[【(][^\]】)]{0,10}사설|^\s*사설\b")


def is_editorial(title):
    return bool(EDITORIAL_TAG.search(title))


# ─────────────────────────────────────────────────────────────
# Bing News RSS → 최신 사설 후보
# ─────────────────────────────────────────────────────────────
def bing_items(domain):
    seen, out = set(), []
    for q in (f'"[사설]" site:{domain}', f'사설 site:{domain}', f'"[기고]" OR "논설" site:{domain}'):
        url = f"https://www.bing.com/news/search?q={urllib.parse.quote(q)}&format=RSS&setLang=ko"
        try:
            d = fetch(url).decode("utf-8", "replace")
        except Exception as e:
            print(f"  [bing err {domain}: {e}]", file=sys.stderr)
            continue
        d = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", d, flags=re.S)
        for it in re.findall(r"<item>(.*?)</item>", d, re.S):
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            l = re.search(r"<link>(.*?)</link>", it, re.S)
            p = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
            if not (t and l):
                continue
            title = H.unescape(t.group(1)).strip()
            if not is_editorial(title):
                continue
            link = H.unescape(l.group(1)).strip()
            m = re.search(r"[?&]url=([^&]+)", link)
            real = urllib.parse.unquote(m.group(1)) if m else link
            pub = None
            if p:
                try:
                    pub = parsedate_to_datetime(p.group(1).strip())
                    if pub.tzinfo is None:
                        pub = pub.replace(tzinfo=timezone.utc)
                    pub = pub.astimezone(KST)
                except Exception:
                    pub = None
            if pub and TODAY - pub > timedelta(hours=MAX_AGE_H):
                continue
            if real in seen:
                continue
            seen.add(real)
            out.append({"title": title, "url": real, "pub": pub.isoformat() if pub else None})
        if len(out) >= PER_OUTLET_TRY * 2:
            break
        time.sleep(0.6)
    return out


# ─────────────────────────────────────────────────────────────
# 본문 추출 (HTML 파서) + 광고/인쇄 등 UI 문구 제거
# ─────────────────────────────────────────────────────────────
def decode_page(url):
    raw = fetch(url, 25)
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def jsonld_body(d):
    for m in re.finditer(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', d):
        try:
            txt = json.loads('"' + m.group(1) + '"')
            if nospace(txt) > 300:
                return txt
        except Exception:
            continue
    return None


GENERIC_MARKER = re.compile(
    r"article[-_]?(?:body|txt|text|view|content)|art_body|news[-_]?body|news_body_area|"
    r"news-view|plain_txt|txt_article|ab_paragraph|story[-_]?body", re.I)
MARKERS = {  # 신문사별 본문 컨테이너(id/class) 패턴 — 없으면 GENERIC_MARKER
    "chosun.com": re.compile(r"article-body|article_body", re.I),
    "hani.co.kr": re.compile(r"article-text|article_text", re.I),
    "donga.com": re.compile(r"article_txt", re.I),
    "munhwa.com": re.compile(r"news_body_area|articlebody", re.I),
    "mk.co.kr": re.compile(r"art_body|plain_txt|txt_article", re.I),
    "hankyung.com": re.compile(r"news-view|article-body", re.I),
    "segye.com": re.compile(r"article_body|articlebody", re.I),
}

SKIP_TAGS = {"script", "style", "noscript", "figure", "figcaption", "aside", "nav", "button",
             "iframe", "ins", "form", "header", "footer", "select", "svg"}
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "area", "base", "col",
             "embed", "param", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "li", "tr", "section", "article", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6"}
BAD_ATTR = re.compile(
    r"(?:^|[-_\s])(?:ad|ads|adv|advert|advertisement|banner|share|sns|print|font|fontsize|related|"
    r"reporter|byline|copyright|btn|button|comment|reply|recommend|subscribe|toolbar|social|"
    r"promo|popular|ranking|taboola|dable|newsletter)(?:$|[-_\s\d])", re.I)


class BodyGrabber(HTMLParser):
    """marker 에 맞는 첫 요소의 하위 텍스트만 모은다 (중첩 태그 깊이 추적, 광고·버튼류는 건너뜀)."""

    def __init__(self, marker):
        super().__init__(convert_charrefs=True)
        self.marker = marker
        self.capturing = False
        self.done = False
        self.depth = 0
        self.skip = 0
        self.buf = []

    @staticmethod
    def _attr_text(attrs):
        d = dict(attrs)
        return f'{d.get("id") or ""} {d.get("class") or ""}'

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        a = self._attr_text(attrs)
        if not self.capturing:
            if tag not in VOID_TAGS and self.marker.search(a):
                self.capturing, self.depth, self.skip = True, 1, 0
            return
        if tag in VOID_TAGS:
            if tag == "br" and not self.skip:
                self.buf.append("\n")
            return
        self.depth += 1
        if self.skip:
            self.skip += 1
            return
        if tag in SKIP_TAGS or BAD_ATTR.search(a):
            self.skip = 1
            return
        if tag in BLOCK_TAGS:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if not self.capturing or tag in VOID_TAGS:
            return
        if self.skip:
            self.skip -= 1
        elif tag in BLOCK_TAGS:
            self.buf.append("\n")
        self.depth -= 1
        if self.depth <= 0:
            self.capturing, self.done = False, True

    def handle_data(self, data):
        if self.capturing and not self.skip:
            self.buf.append(data)


NOISE = re.compile(
    r"(www\.|https?://|ⓒ|©|Copyright|무단\s*전재|재배포|저작권|이메일|@[\w.-]+\.|"
    r"주소\s*:|전화\s*:|팩스\s*:|등록번호|등록일자|발행인|편집인|사업자|통신판매|대표자명|\$\{|"
    r"구독\s*후\s*열람|회원이\s*되어|회원에게만|"
    r"입력\s*\d{4}|수정\s*\d{4}|\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\s+\d{1,2}:\d{2}|구독하기|제보하기)", re.I)
UI_WORDS = re.compile(
    r"광고|인쇄|프린트|스크랩|글자\s*크기|글씨\s*크기|가\s*가|공유하기|공유|페이스북|트위터|엑스|카카오톡|"
    r"카카오스토리|네이버|링크\s*복사|URL\s*복사|주소\s*복사|저장하기|기사\s*저장|댓글|구독|관련\s*기사|"
    r"추천\s*기사|많이\s*본|뉴스레터|좋아요|본문\s*듣기|음성\s*듣기|번역|기사\s*목록|목록|닫기|더보기|"
    r"이전\s*기사|다음\s*기사|바로가기|메일|복사|보내기", re.I)
BYLINE = re.compile(r"((논설|선임|수석|전문|편집|객원)?위원|기자|특파원|주필|칼럼니스트|교수|대표)\s*$")
CAPTION = re.compile(r"^\s*[\[(]?\s*(사진|그래픽|일러스트|이미지|자료|영상)\s*[=:/)\]]")


# ─────────────────────────────────────────────────────────────
# 기사가 아닌 조각 제거 (내비게이션·머리기사 목록·요약문구·브랜드 표기)
#   예) '구글에서 서울신문 먼저 보기', '#대출규제 풍선 효과',
#       '엠에이이아이엘 비유에스아이엔이에스에스 …'(영문 브랜드 한글 표기),
#       '2026년 11월 28일 (토)', '1 [단독] 편의점 한다던 남편 …'(머리기사 목록),
#       '세줄 요약', '부동산 많이 본 기사'
#   원칙: 본문 문장은 건드리지 않는다 — 명백한 라벨/머리기사/브랜드 문구만 제거.
# ─────────────────────────────────────────────────────────────
LETTER_NAME = re.compile(
    r"^(?:에이|이|엘|비|씨|디|에프|지|에이치|아이|제이|케이|엠|엔|오|피|큐|알|에스|티|유|브이|더블유|"
    r"엑스|와이|제트|\s)+[.。]?$")
# 두 단어 이상 붙은 브랜드·서비스 문구 — 일반 문장에 거의 나오지 않는 조합만 넣는다
BRAND_NOISE = re.compile(
    r"(무단\s*전재|재배포\s*금지|저작권자|먼저\s*보기|에서\s*\S{0,12}\s*팔로우|"
    r"많이\s*본|인기\s*기사|추천\s*기사|관련\s*기사|주요\s*뉴스|헤드라인\s*뉴스|"
    r"매경플러스|엘아이브이이|엠블록레터|콘텐츠\s*에이치유비|매의\s*부리처럼|"
    r"쉽지만\s*얕지\s*않게|나만의\s*에이아이|월가에서\s*월급|실리콘밸리\s*현장|"
    r"이미지\s*(확대|크게)|원본\s*보기|세줄\s*요약|핵심\s*요약|카드뉴스|포토뉴스|"
    r"글자\s*크기|본문\s*듣기|기사\s*저장|스크랩하기|티오피\s*\d+)", re.I)
# 짧은 홍보·구독 줄에만 적용 (본문 문장에 흔히 나오는 단어라 길이 제한)
PROMO_SHORT = re.compile(r"(텔레그램|유튜브|뉴스레터|구독하기|구독\s*신청|팔로우|앱\s*설치|바로가기)")
HASHTAG = re.compile(r"(^|\s)#[^\s#]{2,}")
DATELINE = re.compile(r"^\d{4}년\s*\d{1,2}월\s*\d{1,2}일")
NUMBERED = re.compile(r"^[1-9]\d?[.)]\s")
SENT_END = re.compile(r"(다|요|까|죠|네|냐|자|니|오|라|세|군|음|임)[.!?\"'”’)\]」』]*$")


def is_news_noise(seg, fragments=True):
    """기사 본문이 아닌 조각인지 판정.

    fragments=True  : 머리기사 조각·메뉴 라벨까지 제거 (사설·논설 수집용)
    fragments=False : 명백한 브랜드·홍보 문구만 제거 (연설문 등 문단 보존)
    """
    t = seg.strip()
    if not t:
        return True
    if BRAND_NOISE.search(t) or HASHTAG.search(t) or DATELINE.match(t) or LETTER_NAME.match(t):
        return True
    if re.fullmatch(r"[\s\d\W]*", t):
        return True
    n = nospace(t)
    if n <= 20 and PROMO_SHORT.search(t):
        return True
    if fragments:
        if NUMBERED.match(t) and n <= 110:
            return True                                  # '2 3개월만에 …적립금.국내주식…'
        if not SENT_END.search(t) and n <= 12:
            return True                                  # 오피니언 / 라이프 / 티오피50
        if not SENT_END.search(t) and n <= 45:
            return True                                  # '3국 정보 활용 검토 논란도.'
    return False


def strip_news_noise(text, fragments=True):
    """본문이 아닌 줄·문장을 걷어낸다. 140자 넘는 한 줄은 문장 단위로 쪼개서 판정."""
    if not text:
        return text
    items = []          # (원래 줄 번호, 문장)
    for li, ln in enumerate(str(text).split("\n")):
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) <= 140:
            items.append((li, ln))
        else:
            for part in re.split(r"(?<=[.!?])\s+", ln):
                part = part.strip()
                if part:
                    items.append((li, part))
    if not items:
        return ""

    out, seen = [], set()
    for li, seg in items:
        if is_news_noise(seg, fragments):
            continue
        key = re.sub(r"\s+", "", seg)[:60]
        if key in seen:                                  # 같은 문장이 요약·본문에 중복될 때
            continue
        seen.add(key)
        if out and out[-1][0] == li:
            out[-1][1] += " " + seg
        else:
            out.append([li, seg])
    return "\n".join(s for _, s in out).strip()


def clean_text(raw, html_mode=False):
    text = raw
    if html_mode:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S)
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"</(p|div)>", "\n", text)
        text = H.unescape(re.sub(r"<[^>]+>", "", text))
    lines = []
    for ln in (re.sub(r"[ \t\u00a0\u3000]+", " ", l).strip() for l in text.split("\n")):
        if not ln or len(ln) <= 1:
            continue
        if NOISE.search(ln):
            continue
        if ln.startswith(("▲", "▶", "※", "◆", "■", "→", "-", "☞", "▷", "◇")):
            continue
        if CAPTION.search(ln):
            continue
        if len(ln) <= 40:
            rest = UI_WORDS.sub("", ln)
            if len(re.sub(r"[\W\d_]", "", rest)) <= 2:          # 'UI 단어'만으로 된 줄
                continue
            if len(ln) <= 25 and BYLINE.search(ln) and not re.search(r"[.!?다]$", ln):
                continue                                        # '홍길동 논설위원' 류
        if re.fullmatch(r"[\s\d\W]*", ln):
            continue
        lines.append(ln)
    return strip_news_noise("\n".join(lines).strip())


def extract_text(url, base_domain):
    try:
        if not re.search(rf"https?://(www\.)?([a-z0-9-]+\.)*{re.escape(base_domain)}(/|$)", url):
            return None
        d = decode_page(url)
        jb = jsonld_body(d)
        if jb:
            txt = clean_text(jb, html_mode=bool(re.search(r"</?(p|br|div|span)\b", jb)))
            if nospace(txt) > 300:
                return txt
        for mk in (MARKERS.get(base_domain), GENERIC_MARKER):
            if not mk:
                continue
            g = BodyGrabber(mk)
            try:
                g.feed(d)
                g.close()
            except Exception:
                pass
            txt = clean_text("".join(g.buf))
            if nospace(txt) > 300:
                return txt
        paras = [p for p in re.findall(r"<p[^>]*>(.*?)</p>", d, re.S)
                 if len(re.sub(r"<[^>]+>|\s", "", p)) > 30]
        if paras:
            txt = clean_text("\n".join(paras), html_mode=True)
            if nospace(txt) > 300:
                return txt
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 길이 맞춤: 700 ≤ 공백제외 글자수 < 1,100 (초과분은 문장 단위로 자동발췌)
# ─────────────────────────────────────────────────────────────
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"”’'」)])\s+")


def fit_length(text):
    """returns (본문, 발췌여부). 700자 미만이면 (None, False)."""
    total = nospace(text)
    if total < MIN_CHARS:
        return None, False
    if total < MAX_EXCL:
        return text, False
    out, used, stopped = [], 0, False
    for para in text.split("\n"):
        cur = []
        for s in SENT_SPLIT.split(para):
            n = nospace(s)
            if used + n > EXCERPT_CAP:
                stopped = True
                break
            cur.append(s)
            used += n
        if cur:
            out.append(" ".join(cur))
        if stopped:
            break
    res = "\n".join(out).strip()
    return (res, True) if MIN_CHARS <= nospace(res) < MAX_EXCL else (None, False)


# ─────────────────────────────────────────────────────────────
# 중복(과거) / 주제 중복(당일) 판정
# ─────────────────────────────────────────────────────────────
TRACK_PARAMS = ("fbclid", "gclid", "ref", "from", "cmpid")


def norm_url(u):
    p = urllib.parse.urlsplit(u)
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    q = sorted((k, v) for k, v in urllib.parse.parse_qsl(p.query)
               if not k.lower().startswith("utm_") and k.lower() not in TRACK_PARAMS)
    return host + p.path.rstrip("/") + ("?" + urllib.parse.urlencode(q) if q else "")


def title_key(t):
    return re.sub(r"[^0-9a-z가-힣]", "", clean_title(t).lower())


def body_key(text):
    return re.sub(r"[^0-9a-z가-힣]", "", (text or "").lower())[:60]


def _pick_items(j):
    """과거 JSON의 여러 형식을 모두 지원: v4/v5 dict(picked), 구버전 list(한글 키), 'text' 키 변형."""
    if isinstance(j, dict):
        items = j.get("picked") or j.get("articles") or []
    elif isinstance(j, list):
        items = j
    else:
        items = []
    for p in items:
        if not isinstance(p, dict):
            continue
        yield (p.get("url") or p.get("원문 URL") or "",
               p.get("title") or p.get("제목") or "",
               p.get("body") or p.get("text") or p.get("본문") or "")


def _scan_broken(raw):
    """JSON이 깨진 파일은 정규식으로 URL/제목/본문만 건져낸다."""
    for m in re.finditer(r'"(?:url|원문 URL)"\s*:\s*"([^"]+)"', raw):
        yield m.group(1), "", ""
    for m in re.finditer(r'"(?:title|제목)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
        yield "", m.group(1), ""
    for m in re.finditer(r'"(?:body|text|본문)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
        try:
            yield "", "", json.loads('"' + m.group(1) + '"')
        except Exception:
            continue


def load_history():
    urls, titles, bodies = set(), set(), set()
    today_file = f"{TODAY.strftime('%Y-%m-%d')}.json"
    for f in glob(os.path.join(DATA_DIR, "*.json")):
        if os.path.basename(f) == today_file:      # 같은 날 재실행 시 덮어쓰기 허용
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                raw = fh.read()
        except Exception:
            continue
        try:
            entries = list(_pick_items(json.loads(raw)))
        except Exception as e:
            print(f"  [history warn {os.path.basename(f)}: JSON 형식 오류 → 부분 복구] {e}", file=sys.stderr)
            entries = list(_scan_broken(raw))
        for url, title, body in entries:
            if url:
                urls.add(norm_url(url))
            if title:
                titles.add(title_key(title))
            if body:
                bodies.add(body_key(body))
    return {"urls": urls, "titles": titles, "bodies": bodies}


JOSA = re.compile(r"(으로써|으로서|에서는|에게는|이라는|이라고|으로|에서|에게|까지|부터|처럼|보다|마저|조차|"
                  r"라는|라고|이다|은|는|이|가|을|를|의|에|도|로|과|와|만|들)$")
STOP = set("그리고 하지만 그러나 이번 최근 지난 대한 위해 통해 따라 이후 이런 이는 있는 있다 한다 했다 하는 "
           "것이 것은 우리 정부 국민 사회 한국 대통령 때문 경우 가운데 대해 관련 지금 현재 올해 내년 "
           "필요 문제 상황 사실 결과 위한 같은 모든 다른 여러 어떤 이미 아직 오히려 특히 또한".split())


def tokens(text):
    out = []
    for w in re.findall(r"[가-힣]{2,}", text or ""):
        w2 = JOSA.sub("", w) if len(w) > 2 else w
        if len(w2) >= 2 and w2 not in STOP:
            out.append(w2)
    return out


def topic_profile(title, body):
    return set(tokens(title)), {w for w, _ in Counter(tokens(body)).most_common(25)}


def same_topic(a, b):
    """제목 핵심어 3개 이상 공유 / 제목 Jaccard ≥ .34 / 본문 상위 키워드 Jaccard ≥ .30 이면 같은 주제."""
    ta, ka = a
    tb, kb = b
    jt = len(ta & tb) / max(1, len(ta | tb))
    jk = len(ka & kb) / max(1, len(ka | kb))
    return len(ta & tb) >= 3 or jt >= 0.34 or jk >= 0.30


# ─────────────────────────────────────────────────────────────
def collect_outlet(name, domain, hist, min_chars=MIN_CHARS, allow_excerpt=True):
    cands = []
    for c in bing_items(domain):
        if len(cands) >= PER_OUTLET_TRY:
            break
        if norm_url(c["url"]) in hist["urls"] or title_key(c["title"]) in hist["titles"]:
            continue
        txt = extract_text(c["url"], domain)
        if not txt:
            continue
        txt = polish_text(normalize_text(strip_annotations(txt)))
        orig = nospace(txt)
        if orig < min_chars:
            continue
        if orig >= MAX_EXCL:
            if not allow_excerpt:
                continue
            fitted, excerpted = fit_length(txt)
        else:
            fitted, excerpted = txt, False
        if not fitted or body_key(fitted) in hist["bodies"]:
            continue
        ctitle = clean_title(c["title"])
        cands.append({
            "outlet": name, "url": c["url"], "pub": c["pub"],
            "title_full": with_outlet(name, c["title"]),
            "text": fitted, "chars": nospace(fitted), "orig_chars": orig,
            "excerpted": excerpted, "profile": topic_profile(ctitle, fitted),
        })
        time.sleep(0.5)
    cands.sort(key=lambda x: (x["excerpted"], abs(x["chars"] - TARGET_CHARS)))   # 발췌 없는 것 우선
    return cands


def main():
    print(f"=== 소담 사설 수집 v5 {TODAY.strftime('%Y-%m-%d %H:%M')} KST ===")
    hist = load_history()
    print(f"과거 수집분: URL {len(hist['urls'])}건 / 제목 {len(hist['titles'])}건")

    order = list(OUTLETS.items())
    random.Random(TODAY.strftime("%Y%m%d")).shuffle(order)      # 신문사 순서 편향 제거

    chosen, topic_skipped = [], 0
    for name, domain in order:
        if len(chosen) >= PICK:
            break
        for c in collect_outlet(name, domain, hist):
            if any(same_topic(c["profile"], x["profile"]) for x in chosen):
                topic_skipped += 1
                continue
            chosen.append(c)                                    # 신문사당 1편
            break
        time.sleep(1.0)

    # 미달 시 완화 조건으로 보충 (동일주제 금지 · 과거 중복 금지 · 1,100자 미만 유지)
    def relaxed_fill(min_chars):
        added = 0
        for name, domain in order:
            if len(chosen) >= PICK:
                break
            for c in collect_outlet(name, domain, hist, min_chars=min_chars, allow_excerpt=False):
                if any(same_topic(c["profile"], x["profile"]) for x in chosen):
                    continue
                c["relaxed"] = True
                chosen.append(c)
                added += 1
                break
            time.sleep(1.0)
        return added

    if len(chosen) < PICK:
        relaxed_fill(300)
    if len(chosen) < PICK:
        relaxed_fill(150)

    chosen.sort(key=lambda x: x["chars"])                       # 길이 오름차순 1~10번
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, f"{TODAY.strftime('%Y-%m-%d')}.json")
    picked = []
    for i, p in enumerate(chosen, 1):
        n = p["chars"]
        picked.append({
            "no": i,
            "date": TODAY.strftime("%Y-%m-%d"),
            "title": p["title_full"],
            "src": p["outlet"],
            "chars": n,
            "orig_chars": p["orig_chars"],
            "excerpted": p["excerpted"],
            "relaxed": bool(p.get("relaxed")),
            "lv": 1 if n <= 850 else (2 if n <= 1000 else 3),
            "pub": p["pub"],
            "url": p["url"],
            "body": p["text"],
        })
    out = {
        "date": TODAY.strftime("%Y-%m-%d"),
        "criteria": "700~1,099자(공백 제외) · 초과분은 문장 단위 자동발췌 · 과거 수집분/동일 주제/동일 신문사 제외 · "
                    "미달분은 완화 조건(동일주제X·과거중복X·1,100자 미만)으로 보충 · 길이 오름차순 1~10번",
        "picked": picked,
        "summary": [{"no": p["no"], "outlet": p["src"], "title": p["title"], "chars": p["chars"],
                     "excerpted": p["excerpted"], "url": p["url"]} for p in picked],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for p in picked:
        tag = f' (발췌, 원문 {p["orig_chars"]}자)' if p["excerpted"] else ""
        if p.get("relaxed"):
            tag += " (완화)"
        print(f'{p["no"]:>2}. [{p["src"]}] {p["chars"]}자{tag} | {p["title"][:60]}')
    print(f"\n최종 {len(picked)}편 / 주제 중복으로 건너뜀 {topic_skipped}건")
    if len(picked) < PICK:
        print(f"⚠ 조건을 만족하는 사설이 {PICK}편에 못 미칩니다 ({len(picked)}편). 로그의 bing err를 확인하세요.",
              file=sys.stderr)


if __name__ == "__main__":
    main()

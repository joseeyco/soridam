# -*- coding: utf-8 -*-
"""소담 사설 자동 수집기 v2

정리 규칙
1) 본문 공백 제외 1100자를 넘으면 문장 단위로 자연스럽게 잘라 1100자 이내로 맞춘다.
2) 하루 수집분은 주제가 겹치지 않도록 클러스터링해서 뽑는다.
3) 같은 신문사는 하루에 한 번만 넣는다.
4) 제목 맨앞에 [신문사명]을 붙인다.
5) 어떤 상황에서도 10편을 채운다(후보가 부족하면 조건을 단계적으로 완화).

사용 예)
  python3 collect_editorials.py --date 2026-11-21 --window 3
  python3 collect_editorials.py --date 2026-11-21 --window 3 --dump-pool
"""
import argparse
import copy
import html as html_lib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_INTERVAL = 1.0
CHAR_LIMIT = 1100          # 규칙 1: 공백 제외 1100자 상한
MIN_CHARS = 400            # 이보다 짧으면 사설 본문으로 보지 않는다
TARGET_COUNT = 10          # 규칙 5

OUTLETS = [
    ("조선일보", "chosun.com"),
    ("중앙일보", "joongang.co.kr"),
    ("동아일보", "donga.com"),
    ("한겨레", "hani.co.kr"),
    ("경향신문", "khan.co.kr"),
    ("문화일보", "munhwa.com"),
    ("서울신문", "seoul.co.kr"),
    ("국민일보", "kmib.co.kr"),
    ("세계일보", "segye.com"),
    ("매일경제", "mk.co.kr"),
    ("한국경제", "hankyung.com"),
    ("프레시안", "pressian.com"),
    ("헤럴드경제", "heraldcorp.com"),
    ("아시아경제", "asiae.co.kr"),
    ("이데일리", "edaily.co.kr"),
]

# RSS가 오래된 글만 주는 신문사는 사설 목록 페이지에서 직접 긁는다.
LIST_PAGES = {
    "donga.com": "https://www.donga.com/news/Opinion/editorial",
    "munhwa.com": "https://www.munhwa.com/news/section_list.html?sec=opinion",
    "pressian.com": "https://www.pressian.com/pages/opinion",
}

_last_request_at = 0.0


# ---------------------------------------------------------------- 유틸
def compact_length(value):
    return len(re.sub(r"\s+", "", value or ""))


def normalize_text(value):
    value = html_lib.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def throttle():
    global _last_request_at
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def decode_bytes(raw, content_type):
    encodings = []
    match = re.search(r"charset=([\w-]+)", content_type or "", flags=re.I)
    if match:
        encodings.append(match.group(1))
    head = raw[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([\w-]+)", head, flags=re.I)
    if match:
        encodings.append(match.group(1))
    encodings.extend(["utf-8", "euc-kr", "cp949"])
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch(url, timeout=25):
    throttle()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        raw = response.read()
        return response.status, decode_bytes(raw, response.headers.get("Content-Type", "")), response.geturl()


# ---------------------------------------------------------------- 후보 수집
def decode_bing_link(link):
    parsed = urllib.parse.urlparse(link or "")
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("url", "u"):
        for value in query.get(key, []):
            if value.startswith("http"):
                return value
    match = re.search(r"[?&]url=([^&]+)", link or "")
    return urllib.parse.unquote(match.group(1)) if match else (link or "")


def domain_matches(url, domain):
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    return host == domain or host.endswith("." + domain)


def normalized_title(value):
    return re.sub(r"\s+", "", html_lib.unescape(value or "")).strip("[]()")


def bing_items(domain):
    query = urllib.parse.quote_plus(f"[사설] site:{domain}")
    rss_url = f"https://www.bing.com/news/search?q={query}&format=RSS&setLang=ko"
    _, body, _ = fetch(rss_url)
    root = ET.fromstring(body)
    items, seen = [], set()
    for node in root.findall(".//item"):
        title = normalize_text(node.findtext("title") or "")
        actual_url = decode_bing_link((node.findtext("link") or "").strip())
        if not actual_url or not domain_matches(actual_url, domain):
            continue
        key = re.sub(r"\?.*$", "", actual_url)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title,
            "url": actual_url,
            "published": (node.findtext("pubDate") or "").strip(),
        })
    return rss_url, items


def list_page_items(domain):
    url = LIST_PAGES.get(domain)
    if not url:
        return "", []
    _, page, final = fetch(url)
    soup = BeautifulSoup(page, "lxml")
    items, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        title = normalize_text(anchor.get_text(" ", strip=True))
        if "사설" not in title or compact_length(title) < 8:
            continue
        href = urljoin(final, anchor["href"])
        if not domain_matches(href, domain) or "bing.com" in href:
            continue
        key = re.sub(r"\?.*$", "", href)
        if key in seen:
            continue
        seen.add(key)
        items.append({"title": title, "url": href, "published": ""})
    return url, items


# ---------------------------------------------------------------- 본문 추출
def walk_json_ld(value):
    if isinstance(value, dict):
        body = value.get("articleBody")
        if isinstance(body, str) and body.strip():
            yield body
        for child in value.values():
            yield from walk_json_ld(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json_ld(child)


def extract_json_ld_body(page):
    soup = BeautifulSoup(page, "lxml")
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", raw).strip()
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
        for body in walk_json_ld(data):
            text = normalize_text(body)
            if compact_length(text) >= MIN_CHARS:
                return text
    return ""


def clean_node(node, remove_selectors):
    copied = copy.copy(node)
    for selector in remove_selectors:
        for child in copied.select(selector):
            child.decompose()
    return normalize_text(copied.get_text(" ", strip=True))


def paragraph_texts(node, min_length=30, excluded_ancestors=None):
    excluded_ancestors = excluded_ancestors or []
    values = []
    for paragraph in node.find_all("p"):
        text = normalize_text(paragraph.get_text(" ", strip=True))
        if compact_length(text) < min_length:
            continue
        if is_ui_chrome(text):
            continue
        excluded = False
        for ancestor in paragraph.parents:
            if not getattr(ancestor, "name", None):
                continue
            marker = " ".join([ancestor.get("id") or "", " ".join(ancestor.get("class") or [])]).lower()
            if any(token in marker for token in excluded_ancestors):
                excluded = True
                break
        if not excluded:
            values.append(text)
    return values


def extract_chosun(page):
    soup = BeautifulSoup(page, "lxml")
    best = ""
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if '"content":' not in raw:
            continue
        values = []
        for match in re.finditer(r'"content":("(?:\\.|[^"\\])*")', raw):
            try:
                value = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if compact_length(value) >= 40:
                values.append(value)
        text = normalize_text(" ".join(values))
        if compact_length(text) > compact_length(best):
            best = text
    return best


def extract_outlet_selector(page, domain):
    soup = BeautifulSoup(page, "lxml")
    if domain == "chosun.com":
        return extract_chosun(page)
    mapping = {
        "joongang.co.kr": "#article_body",
        "donga.com": "section.news_view",
        "hani.co.kr": ".article-text",
        "khan.co.kr": "#articleBody",
        "munhwa.com": "#article-body",
        "seoul.co.kr": "#articleContent",
        "kmib.co.kr": "#articleBody",
        "segye.com": "#article_txt",
        "mk.co.kr": ".news_cnt_detail_wrap",
        "hankyung.com": ".article-body",
        "pressian.com": ".article_body",
        "heraldcorp.com": "article#articleText, article.article-body, article.article-view",
        "asiae.co.kr": ".article_view",
        "edaily.co.kr": ".news_body",
    }
    selector = mapping.get(domain)
    if not selector:
        return ""
    node = soup.select_one(selector)
    if not node:
        return ""
    excluded = ["footer", "header", "nav", "rank", "card", "swiper", "recommend", "related",
                "policy-box", "subscribe", "adwrap", "imagecontainer", "photo", "caption"]
    paragraphs = paragraph_texts(node, min_length=30, excluded_ancestors=excluded)
    if paragraphs and domain != "donga.com":
        return normalize_text(" ".join(paragraphs))
    return clean_node(node, ["figure", "script", "style", ".ad", ".ad-txt-picbtm"])


def drop_chrome_containers(soup):
    for node in soup.find_all(["section", "div", "p", "ul", "aside"]):
        marker = " ".join([node.get("id") or "", " ".join(node.get("class") or [])]).lower()
        if any(hint in marker for hint in CHROME_CONTAINER_HINTS):
            node.decompose()
    return soup


def extract_paragraph_cluster(page):
    soup = drop_chrome_containers(BeautifulSoup(page, "lxml"))
    excluded = ["footer", "header", "nav", "rank", "card", "swiper", "recommend",
                "related", "policy-box", "subscribe"]
    groups = {}
    for paragraph in soup.find_all("p"):
        text = normalize_text(paragraph.get_text(" ", strip=True))
        if compact_length(text) < 30:
            continue
        parent = paragraph.find_parent(["div", "article", "section"])
        if not parent:
            continue
        marker = " ".join([parent.get("id") or "", " ".join(parent.get("class") or [])]).lower()
        if any(token in marker for token in excluded):
            continue
        key = (parent.name, parent.get("id") or "", " ".join(parent.get("class") or []))
        groups.setdefault(key, []).append(text)
    candidates = [normalize_text(" ".join(v)) for v in groups.values()]
    candidates = [t for t in candidates if compact_length(t) >= MIN_CHARS]
    if not candidates:
        return ""
    candidates.sort(key=lambda t: abs(compact_length(t) - 950))
    return candidates[0]



# ---------------------------------------------------------------- 본문 아님(UI 크롬) 제거
# 기사 본문이 아니라 기사 페이지의 조작 버튼·안내 문구. 어떤 경로로 딸려 들어와도 지운다.
UI_CHROME_PATTERNS = [
    r"(?<![가-힣])인쇄(?:하기)?(?![가-힣])",
    r"(?<![가-힣])메일(?:로\s*보내기|보내기)?(?![가-힣])",
    r"페이스북\s*(?:공유|로그인)",
    r"트위터\s*(?:공유|로그인)",
    r"(?:url|URL|링크|카카오톡|카카오|밴드|네이버)\s*(?:공유|복사|보내기)",
    r"(?<![가-힣])공유하기(?![가-힣])",
    r"스크랩(?:하기)?",
    r"글씨\s*크기\s*선택",
    r"가장\s*작은\s*크기\s*글자",
    r"한\s*단계\s*작은\s*크기\s*글자",
    r"기본\s*크기\s*글자",
    r"한\s*단계\s*큰\s*크기\s*글자",
    r"가장\s*큰\s*크기\s*글자",
    r"글자\s*크기\s*(?:조절|변경|확대|축소)",
    r"구글\s*선호\s*매체\s*등록",
    r"선호\s*매체\s*(?:등록|해제)",
    r"구글\s*뉴스(?:에서\s*보기)?",
    r"관련\s*이슈",
    r"입력\s*:\s*\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}",
    r"수정\s*:\s*\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}",
    r"(?<![가-힣])댓글\s*(?:\d+)?\s*(?:보기|쓰기|달기)?(?![가-힣])",
    r"(?<![가-힣])추천\s*\d*(?![가-힣])",
    r"반응\s*\d+",
    r"목록으로|기사\s*목록",
    r"이전\s*기사|다음\s*기사",
    r"많이\s*본\s*뉴스",
    r"이\s*기사를\s*추천합니다",
    r"구독(?:하기|신청)",
    r"무단\s*전재[^.]{0,20}금지",
]
UI_CHROME_RE = re.compile("|".join(f"(?:{p})" for p in UI_CHROME_PATTERNS), re.I)
CHROME_LEFT_MAX = 8           # 크롬 문구를 걷어낸 뒤 남는 글자가 이보다 적으면 본문이 아니다
CHROME_MIN_MARKS = 2          # 크롬 표시가 2개 이상이면 문장 단위로도 버린다


def chrome_marks(text):
    return len(UI_CHROME_RE.findall(text or ""))


def _chrome_leftover(text):
    leftover = UI_CHROME_RE.sub(" ", text or "")
    leftover = re.sub(r"[\s\-+|·,./()\[\]<>\"']", "", leftover)
    return compact_length(leftover)


def is_ui_chrome(text):
    """문장·문단이 본문이 아니라 기사 페이지 버튼/안내문인지 판별.

    크롬 표시가 1개뿐이면('추천' 같은 낱말이 본문에 섞인 경우) 거의 확실한 크롬만 버린다.
    """
    if not text:
        return False
    marks = chrome_marks(text)
    if marks == 0:
        return False
    leftover = _chrome_leftover(text)
    if marks >= 2:
        return leftover <= CHROME_LEFT_MAX
    return leftover <= 3


def strip_ui_chrome(text):
    """문장 단위로 크롬 문구를 제거하고, 크롬 덩어리 문장은 통째로 버린다."""
    if not text:
        return text
    kept = []
    for sentence in split_sentences(text):
        marks = chrome_marks(sentence)
        if marks == 0:
            kept.append(sentence)
            continue
        leftover = _chrome_leftover(sentence)
        if (leftover <= CHROME_LEFT_MAX if marks >= 2 else leftover <= 3):
            continue                      # 통째로 크롬 → 버린다
        if marks >= 3 and leftover <= 60:
            # 크롬 덩어리에 본문 한 문장이 붙어 있는 경우만 그 문장만 남긴다
            cleaned = UI_CHROME_RE.sub(" ", sentence)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,·-|+")
            if compact_length(cleaned) >= 12:
                kept.append(cleaned)
            continue
        kept.append(sentence)             # 본문 문장 → 손대지 않는다
    return re.sub(r"\s{2,}", " ", " ".join(kept)).strip()


CAPTION_MARKERS = ["포즈를 취한", "사진기자", "연합뉴스", "로이터", "원본보기", "이미지 확대", "캡션", "자료사진"]

COLUMN_MARKERS = ["겜스파일", "공간과 스타일", "주간스타트업동향", "기자]", "포토", "영상"]

CHROME_CONTAINER_HINTS = ["viewhelp", "conttitle", "share", "sns", "fontsize", "font-size",
                          "font_size", "print", "byline", "reporter", "articlehead", "newshead",
                          "preferred-source", "toolbar"]

BOILERPLATE = [
    r"<ⓒ[^>]*>",
    r"ⓒ\s*[^.]{0,60}(무단전재|무단 전재)[^.]*\.?",
    r"Copyright\s*ⓒ[^.]*\.?",
    r"저작권자\s*ⓒ[^.]*\.?",
    r"무단\s*전재\s*(및|·)?\s*재배포\s*금지[^.]*\.?",
    r"AI\s*학습\s*및\s*활용\s*금지[^>]*>?",
    r"[가-힣A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"\d{4}-\d{2}-\d{2}\s*\d+면",
    r"구글에서\s+\S+\s+먼저\s+보기",
    r"^\s*\[[^\]]{1,12}\]\s*$",
    r"무단\s*전재\s*[-–]?\s*재배포[^.]{0,20}$",
    r"View\s*English\s*Article",
    r"오늘의\s*주요\s*뉴스[^.]{0,40}",
    r"모닝에디트[^.]{0,20}",
    r"이\s*기사를\s*추천합니다[^.]{0,20}",
    r"구독하기[^.]{0,20}",
    r"이런\s*구독물도\s*추천합니다!?",
    r"구독물도\s*추천합니다!?",
    r"\b[가-힣]{2,4}\s*기자\s*$",
    r"\b[가-힣]{2,4}\s*특파원\s*$",
    r"이미지\s*확대\s*(?:보기|닫기)",
    r"원본보기\s*아이콘",
    r"로이터\s*연합뉴스",
    r"(?<=[\s.])AD(?=\s)",
]


def clean_body(text):
    text = normalize_text(text)
    text = strip_ui_chrome(text)
    for pattern in BOILERPLATE:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(r"\([^)]{0,20}(사진|자료사진|연합뉴스|뉴시스|Getty)[^)]{0,20}\)", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = text.strip(" ,·-")
    sentences = split_sentences(text)
    if len(sentences) > 1:
        kept = []
        for idx, sentence in enumerate(sentences):
            if idx < 2 and (compact_length(sentence) < 10 or any(k in sentence for k in CAPTION_MARKERS)):
                continue
            kept.append(sentence)
        text = " ".join(kept)
    endings = list(re.finditer(r"[다요까죠음임]\s*[.!?]|[.!?]\s*['”’]?\s*$", text))
    if endings and endings[-1].end() < len(text) - 1:
        tail = text[endings[-1].end():]
        if compact_length(tail) <= 80:
            text = text[:endings[-1].end()].strip()
    return text.strip(" ,·-")


SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text):
    parts = []
    for chunk in SENTENCE_SPLIT.split(text):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def fit_length(text, limit=CHAR_LIMIT):
    """규칙 1: 1100자를 넘으면 문장 경계에서만 자른다(문장 중간 절단 금지)."""
    if compact_length(text) <= limit:
        return text, False
    kept, total = [], 0
    for sentence in split_sentences(text):
        size = compact_length(sentence)
        if total + size > limit:
            break
        kept.append(sentence)
        total += size
    if not kept:
        return "", True
    trimmed = " ".join(kept).rstrip(" ,·-")
    if not re.search(r"[.!?다요]$", trimmed):
        trimmed = re.sub(r"[\s,·-]+$", "", trimmed)
    return trimmed, True


def strip_editorial_tag(title):
    title = normalize_text(title)
    title = re.sub(r"^\s*[\[\(]?\s*사설\s*[\]\)]?\s*", "", title)
    title = re.sub(r"\s*[\[\(]?\s*사설\s*[\]\)]?\s*$", "", title)
    title = re.sub(r"\s*\[?\s*사설\s*\]?\s*", " ", title)
    return re.sub(r"\s{2,}", " ", title).strip()



# ---------------------------------------------------------------- 주제 사전
# 반복되는 사설 주제를 명시적으로 묶는다(규칙 2). 필요하면 여기에 한 줄씩 추가하면 된다.
TOPIC_RULES = [
    ("수능·AI부정", ["수능", "커닝", "부정행위", "국가시험", "시험지", "감독관"]),
    ("계엄·내란·한덕수", ["한덕수", "내란", "계엄", "파기환송", "추경호"]),
    ("민주유공자법", ["민주유공자"]),
    ("한중FTA·한한령", ["한한령", "한중", "한·중", "FTA", "서비스·투자"]),
    ("수출1조달러", ["1조달러", "1조 달러", "수출"]),
    ("사관학교통합", ["사관학교", "육사"]),
    ("대법원장탄핵", ["대법원장"]),
    ("증시·코스피", ["코스피", "증안펀드", "증시", "주가", "주식시장"]),
    ("외교안보라인·개각", ["개각", "외교안보", "외교·안보", "외교안보라인"]),
    ("정년연장", ["정년"]),
    ("전기차·세액공제", ["전기차", "세액공제"]),
    ("새만금공항·SOC", ["새만금", "SOC"]),
    ("미중러·국제질서", ["미·중·러", "미중러", "국제질서", "강대국"]),
    ("성장률·경제전망", ["KDI", "성장률", "성장 전망"]),
    ("프런티어AI", ["앤스로픽", "프런티어"]),
    ("김민석·여당내분", ["김민석"]),
    ("기업수사·기소", ["기소", "무죄", "수사"]),
]


def topic_label(item):
    text = (item.get("제목", "") + " " + item.get("본문", "")[:400])
    best, best_hits = "", 0
    for label, keywords in TOPIC_RULES:
        hits = sum(1 for keyword in keywords if keyword in text)
        if hits > best_hits:
            best, best_hits = label, hits
    return best

# ---------------------------------------------------------------- 주제 클러스터
STOPWORDS = set("""
사설 오늘 이번 지난 우리나라 우리 정부 국민 정치 사회 경제 대한 위한 관련 이라며 이라고 한다
있는 없다 모두 이제 다시 여전히 매우 아주 가장 또 및 그리고 하지만 그러나 그런데 이날 같은
""".split())


def title_tokens(title):
    text = re.sub(r"^\s*\[[^\]]{2,10}\]\s*", "", title or "")
    text = strip_editorial_tag(text)
    text = re.sub(r"[^가-힣A-Za-z0-9]", " ", text)
    tokens = []
    for word in text.split():
        if len(word) < 2 or word in STOPWORDS:
            continue
        tokens.append(word)
    return set(tokens)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def body_signature(body, k=80):
    text = re.sub(r"[^가-힣A-Za-z0-9]", "", body or "")
    return {text[i:i + 3] for i in range(0, max(0, len(text) - k), 3)}


def char_grams(text, n=2):
    text = re.sub(r"[^가-힣A-Za-z0-9]", "", text or "")
    return {text[i:i + n] for i in range(0, max(0, len(text) - n + 1))}


def same_topic(a, b, threshold=0.18):
    if jaccard(title_tokens(a["제목"]), title_tokens(b["제목"])) >= threshold:
        return True
    if jaccard(char_grams(a["제목"]), char_grams(b["제목"])) >= 0.34:
        return True
    sa, sb = body_signature(a["본문"]), body_signature(b["본문"])
    if not sa or not sb:
        return False
    overlap = len(sa & sb) / min(len(sa), len(sb))
    return overlap >= 0.42


def assign_clusters(items, threshold=0.22):
    clusters = []
    for item in items:
        placed = False
        for cluster in clusters:
            if any(same_topic(item, member, threshold) for member in cluster):
                cluster.append(item)
                item["topic_id"] = clusters.index(cluster) + 1
                placed = True
                break
        if not placed:
            clusters.append([item])
            item["topic_id"] = len(clusters)
    return clusters


# ---------------------------------------------------------------- 수집 파이프라인
def parse_pubdate(value):
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            return dt.astimezone(KST)
        except (ValueError, TypeError):
            continue
    return None


def url_date(url):
    match = re.search(r"/(20\d{2})(\d{2})(\d{2})/", url) or re.search(r"/(20\d{2})-(\d{2})-(\d{2})/", url)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=KST)
        except ValueError:
            return None
    match = re.search(r"article/(20\d{2})(\d{2})(\d{2})", url)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=KST)
        except ValueError:
            return None
    return None


def gather(window_days, reference_date, verbose=True):
    start = reference_date - timedelta(days=window_days)
    end = reference_date + timedelta(days=1)
    pool, log = [], []
    for index, (outlet, domain) in enumerate(OUTLETS, start=1):
        try:
            rss_url, items = bing_items(domain)
        except Exception as error:
            rss_url, items = "", []
            log.append({"outlet": outlet, "stage": "rss", "error": f"{type(error).__name__}: {error}"})
        if len(items) < 3:
            try:
                lp_url, extra = list_page_items(domain)
                known = {i["url"] for i in items}
                items.extend([x for x in extra if x["url"] not in known])
            except Exception as error:
                log.append({"outlet": outlet, "stage": "list", "error": f"{type(error).__name__}: {error}"})
        kept = 0
        for item in items:
            # 사설만: 제목에 '사설' 표기가 있는 기사로 한정(칼럼·기사·어린이동아 등 배제)
            if "사설" not in item["title"]:
                continue
            if any(marker in item["title"] for marker in COLUMN_MARKERS):
                continue
            published = parse_pubdate(item.get("published", "")) or url_date(item["url"])
            if published and not (start <= published <= end):
                continue
            try:
                _, page, final_url = fetch(item["url"])
            except Exception as error:
                log.append({"outlet": outlet, "stage": "fetch", "url": item["url"],
                            "error": f"{type(error).__name__}: {error}"})
                continue
            body, method = "", "none"
            try:
                body, method = extract_json_ld_body(page), "json-ld"
                if compact_length(body) < MIN_CHARS:
                    body, method = "", "none"
                if not body:
                    body = clean_body(extract_outlet_selector(page, domain))
                    method = "selector"
                if compact_length(body) < MIN_CHARS:
                    body = clean_body(extract_paragraph_cluster(page))
                    method = "paragraph-cluster"
            except Exception as error:
                log.append({"outlet": outlet, "stage": "extract", "url": item["url"],
                            "error": f"{type(error).__name__}: {error}"})
                continue
            body = clean_body(body)
            if compact_length(body) < MIN_CHARS:
                continue
            fitted, truncated = fit_length(body)
            if compact_length(fitted) < MIN_CHARS:
                continue
            pool.append({
                "신문사명": outlet,
                "원제목": normalize_text(item["title"]),
                "제목": f"[{outlet}] {strip_editorial_tag(item['title'])}",
                "원문 URL": final_url,
                "발행일": published.strftime("%Y-%m-%d") if published else "",
                "본문": fitted,
                "공백 제외 자수": compact_length(fitted),
                "원문 자수": compact_length(body),
                "1100자 초과 정리": truncated,
                "추출 방식": method,
            })
            kept += 1
            if kept >= 4:
                break
        if verbose:
            print(f"[{index:02d}/{len(OUTLETS)}] {outlet}: 후보 {len(items)}건 중 {kept}건 확보", flush=True)
    assign_clusters(pool)
    for item in pool:
        item["주제"] = topic_label(item) or f"기타-{item['topic_id']}"
    return pool, log


def select(pool, target=TARGET_COUNT):
    """규칙 2·3·5: 주제·신문사 중복 없이 10편. 부족하면 단계적으로 완화."""
    ranked = sorted(pool, key=lambda x: (-min(x["공백 제외 자수"], CHAR_LIMIT), x["발행일"]), reverse=False)
    ranked = sorted(pool, key=lambda x: (x["공백 제외 자수"] < 400, -x["공백 제외 자수"], x["발행일"]), reverse=True)
    selected, outlets, topics = [], set(), set()
    for item in ranked:
        if len(selected) >= target:
            break
        if item["신문사명"] in outlets or item["주제"] in topics:
            continue
        selected.append(item)
        outlets.add(item["신문사명"])
        topics.add(item["주제"])
    stage = 1
    # 1차 완화: 주제 중복 허용, 신문사 중복 금지
    if len(selected) < target:
        stage = 2
        for item in ranked:
            if len(selected) >= target:
                break
            if item in selected or item["신문사명"] in outlets:
                continue
            selected.append(item)
            outlets.add(item["신문사명"])
    # 2차 완화: 신문사 중복까지 허용(같은 신문사 다른 주제만)
    if len(selected) < target:
        stage = 3
        for item in ranked:
            if len(selected) >= target:
                break
            if item in selected:
                continue
            if item["주제"] in {s["주제"] for s in selected}:
                continue
            selected.append(item)
    return selected, stage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(KST).strftime("%Y-%m-%d"))
    parser.add_argument("--window", type=int, default=2)
    parser.add_argument("--out-dir", default="/home/user/output")
    parser.add_argument("--dump-pool", action="store_true")
    parser.add_argument("--reuse-pool", action="store_true")
    parser.add_argument("--recluster", action="store_true")
    args = parser.parse_args()

    reference = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=KST)
    pool_path = os.path.join(args.out_dir, f"pool-{args.date}.json")
    if args.reuse_pool and os.path.exists(pool_path):
        pool = json.load(open(pool_path, encoding="utf-8"))
        log = []
        for item in pool:
            item["주제"] = topic_label(item) or f"기타-{item['topic_id']}"
        print(f"[cache] {pool_path} 재사용 ({len(pool)}건)", flush=True)
    else:
        pool, log = gather(args.window, reference)
        os.makedirs(args.out_dir, exist_ok=True)
        with open(pool_path, "w", encoding="utf-8") as handle:
            json.dump(pool, handle, ensure_ascii=False, indent=1)
    selected, stage = select(pool)

    os.makedirs(args.out_dir, exist_ok=True)
    if args.dump_pool:
        with open(os.path.join(args.out_dir, "pool.json"), "w", encoding="utf-8") as handle:
            json.dump(pool, handle, ensure_ascii=False, indent=1)

    payload = {
        "date": args.date,
        "generated_at_kst": datetime.now(KST).isoformat(),
        "rules": {
            "char_limit_no_space": CHAR_LIMIT,
            "min_chars": MIN_CHARS,
            "target_count": TARGET_COUNT,
            "topic_dedup": True,
            "outlet_dedup": True,
        },
        "topic_clusters": len({item["topic_id"] for item in pool}),
        "picked": [
            {
                "신문사명": item["신문사명"],
                "제목": item["제목"],
                "공백 제외 자수": item["공백 제외 자수"],
                "원문 자수": item["원문 자수"],
                "1100자 초과 정리": item["1100자 초과 정리"],
                "주제 클러스터": item["주제"],
                "발행일": item["발행일"],
                "원문 URL": item["원문 URL"],
                "본문": item["본문"],
            }
            for item in selected
        ],
        "collection_log": log,
    }
    out_path = os.path.join(args.out_dir, f"{args.date}.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload["picked"], handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with open(os.path.join(args.out_dir, f"sodam-editorials-{args.date}.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    lines = [f"# 소담 사설 브리핑 — {args.date}", "",
             f"**{len(selected)}편** · **{len({i['신문사명'] for i in selected})}개 신문사** · "
             f"**{len({i['주제'] for i in selected})}개 주제**", "",
             "| # | 신문사 | 제목 | 자수 | 원문 |", "|---:|---|---|---:|---|"]
    for idx, item in enumerate(selected, start=1):
        lines.append(f"| {idx} | {item['신문사명']} | {item['제목'].replace('|', '\\|')} | "
                     f"{item['공백 제외 자수']} | [원문]({item['원문 URL']}) |")
    lines.append("")
    for idx, item in enumerate(selected, start=1):
        lines.extend([f"## {idx}. {item['제목']}", "",
                      f"- 자수(공백 제외): {item['공백 제외 자수']}자"
                      + (" (1100자 초과 → 문장 단위 정리)" if item["1100자 초과 정리"] else ""),
                      f"- 원문: {item['원문 URL']}", "", item["본문"], ""])
    with open(os.path.join(args.out_dir, f"{args.date}.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    print(json.dumps({
        "date": args.date,
        "picked": len(selected),
        "outlets": [i["신문사명"] for i in selected],
        "topics": [i["주제"] for i in selected],
        "lengths": [i["공백 제외 자수"] for i in selected],
        "truncated": sum(1 for i in selected if i["1100자 초과 정리"]),
        "pool": len(pool),
        "stage": stage,
        "out": out_path,
    }, ensure_ascii=False, indent=2))
    return 0 if len(selected) >= TARGET_COUNT else 2


if __name__ == "__main__":
    sys.exit(main())

"""게시물 URL 을 크롤링해 AI 로 제목·본문·요약을 정리하는 부분.

- 게시판 목록 주소를 넣으면: 같은 사이트 안의 링크 중 개별 게시물 링크를 AI 가 골라, 최대 MAX_POSTS 건까지 읽음
  (한 쪽에서 모자라면 AI 가 고른 다음 페이지 링크를 따라가며, 최대 MAX_LIST_PAGES 쪽까지 읽음.
   다음 쪽 링크가 javascript 라 주소가 없으면, 주소의 쪽 번호(page·pageIndex 등)를 1 올려 읽음)
- 글 하나의 주소를 넣으면: 그 글만 읽음
- 읽은 페이지마다 AI 가 메뉴·바닥글 같은 잡음을 빼고 제목·본문을 뽑고, 본문을 한두 줄로 요약함
- 사이트의 robots.txt(크롤링 허용 규칙)를 지키고, 요청 사이에 잠깐씩 쉼

읽는 방법: requests + 파이썬 기본 html.parser (사용자가 고름, 근거: .claude/rules/upload.md)
"""
import re
import time
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse

import requests

from classifier import ask_json

MAX_POSTS = 100         # 한 번에 읽는 게시물 수 상한 (PRD 비목표 "한꺼번에 전체 분류"와 부딪히지 않게 상한은 둠)
MAX_LIST_PAGES = 10     # 다음 페이지를 따라가며 읽는 게시판 목록 페이지 수 상한
MAX_LINKS_TO_AI = 150   # 게시물 링크를 고르라고 AI 에게 보내는 링크 수 상한
MAX_NAV_LINKS = 60      # 다음 페이지 링크를 고르라고 AI 에게 보내는 짧은 링크(페이지 번호 등) 수 상한
PAGE_TEXT_LIMIT = 4000  # 페이지 하나에서 AI 에게 보내는 글자 수
EXTRACT_CHUNK = 5       # 한 번에 AI 에게 정리를 맡기는 페이지 수
REQUEST_GAP = 0.5       # 같은 사이트에 요청을 보낼 때 사이에 쉬는 초
USER_AGENT = "Mozilla/5.0 (compatible; DataSortlyBot/1.0)"
# 게시판 주소에서 쪽 번호로 흔히 쓰는 이름 — 다음 쪽 링크가 javascript 라 주소가 없을 때, 이 숫자를 1 올려 다음 쪽을 읽음
PAGE_PARAM = re.compile(r"^(page|pageindex|pageno|pagenum|page_no|pg|cpage|curpage|currentpage|nowpage)$", re.I)
SKIP_EXT = (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip",
            ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3")


class CrawlError(Exception):
    """화면에 그대로 보여 줄 안내 문구가 담긴 오류"""


class PageParser(HTMLParser):
    # 페이지의 <title>, 본문 글자, 링크(주소·글자)를 모음 — 스크립트·메뉴·머리말·바닥글 안의 글자와 링크는 뺌
    SKIP = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "td", "th"}

    def __init__(self):
        super().__init__()
        self.title = ""
        self.lines = [""]
        self.links = []  # [(주소, 링크 글자), ...] — 본문 영역 링크
        self.all_links = []  # 메뉴·머리말·바닥글 안의 링크까지 (다음 페이지 링크를 찾을 때 씀)
        self.link_in_skip = False
        self.inputs = []  # [(이름, 값), ...] — 숨은 입력칸에 쪽 번호 이름(pageIndex 등)이 있는지 볼 때 씀
        self.skip_depth = 0
        self.in_title = False
        self.link_href = None
        self.link_text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            a = dict(attrs)
            if a.get("name"):
                self.inputs.append((a["name"], a.get("value") or ""))
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "a":
            self.link_href = dict(attrs).get("href")
            self.link_text = ""
            self.link_in_skip = bool(self.skip_depth)
        if tag in self.BLOCK:
            self.lines.append("")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag == "title":
            self.in_title = False
        elif tag == "a" and self.link_href is not None:
            link = (self.link_href, " ".join(self.link_text.split()))
            self.all_links.append(link)
            if not self.link_in_skip:
                self.links.append(link)
            self.link_href = None
        if tag in self.BLOCK:
            self.lines.append("")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.link_href is not None:
            self.link_text += data
        if not self.skip_depth:
            self.lines[-1] += data

    def text(self):
        lines = (" ".join(line.split()) for line in self.lines)
        return "\n".join(line for line in lines if line)


class Crawler:
    def __init__(self, start_url):
        self.start_url = start_url
        self.site = self._host(start_url)
        self.robots = robotparser.RobotFileParser()
        parts = urlparse(start_url)
        self.robots.set_url(f"{parts.scheme}://{parts.netloc}/robots.txt")
        try:
            res = requests.get(self.robots.url, headers={"User-Agent": USER_AGENT}, timeout=10)
            # robots.txt 가 없으면(404 등) 모두 허용으로 봄
            self.robots.parse(res.text.splitlines() if res.status_code == 200 else [])
        except requests.RequestException:
            self.robots.parse([])
        self.last_request = 0.0

    @staticmethod
    def _host(url):
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    def allowed(self, url):
        return self.robots.can_fetch(USER_AGENT, url)

    def fetch(self, url):
        """페이지를 읽어 PageParser 로 돌려줌"""
        wait = REQUEST_GAP - (time.time() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        self.last_request = time.time()
        res.raise_for_status()
        # 응답에 글자 인코딩이 안 적혀 있으면 requests 가 ISO-8859-1 로 읽어 한글이 깨지므로, 내용으로 추정한 인코딩을 씀
        if "charset" not in res.headers.get("Content-Type", "").lower():
            res.encoding = res.apparent_encoding
        page = PageParser()
        page.feed(res.text)
        return page

    def _site_links(self, raw_links, base_url, skip):
        """같은 사이트 안의 링크만 [(주소, 글자), ...] (나온 순서, 겹침 없이, skip 에 있는 주소는 뺌)"""
        seen, links = set(skip), []
        for href, text in raw_links:
            if not href or not text or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            url = urldefrag(urljoin(base_url, href)).url
            if self._host(url) != self.site or url in seen or urlparse(url).path.lower().endswith(SKIP_EXT):
                continue
            seen.add(url)
            links.append((url, text[:80]))
        return links

    def candidate_links(self, page, base_url, skip=()):
        """게시물 후보 링크 — 본문 영역에서 글자가 4자 이상 붙은 링크만"""
        links = self._site_links(page.links, base_url, {self.start_url, *skip})
        return [(url, text) for url, text in links if len(text) >= 4][:MAX_LINKS_TO_AI]

    @staticmethod
    def numbered_next_url(page, list_url):
        """다음 쪽 링크가 javascript 라 주소가 없을 때 쓰는 대신 방법 —
        주소에 쪽 번호(page·pageIndex 등)가 있으면 1 올리고, 없으면 페이지 숨은 입력칸의 쪽 번호 이름으로 2쪽을 붙임"""
        parts = urlparse(list_url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        for i, (name, value) in enumerate(query):
            if PAGE_PARAM.match(name):
                if not value.isdigit():
                    return None
                query[i] = (name, str(int(value) + 1))
                return parts._replace(query=urlencode(query)).geturl()
        name = next((n for n, _ in page.inputs if PAGE_PARAM.match(n)), None)
        if not name:
            return None
        return parts._replace(query=urlencode(query + [(name, "2")])).geturl()

    def nav_links(self, page, base_url, skip=()):
        """다음 페이지 후보 링크 — 메뉴 영역까지 포함해 글자가 짧은 링크(페이지 번호, 다음, > 등)만"""
        links = self._site_links(page.all_links, base_url, {base_url, *skip})
        return [(url, text) for url, text in links if len(text) <= 10][:MAX_NAV_LINKS]


# ---------- AI 로 게시물 링크 고르기 · 제목·본문·요약 뽑기 ----------

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "post_numbers": {"type": "array", "items": {"type": "integer"}},
        "next_page_number": {"type": "integer"},
    },
    "required": ["post_numbers", "next_page_number"],
    "additionalProperties": False,
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "no": {"type": "integer"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["no", "title", "body", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def pick_post_links(keys, links, nav_links, limit):
    """게시판 목록 페이지의 링크 중 개별 게시물 링크(최대 limit 개)와 다음 페이지 링크를 AI 가 골라
    ([(주소, 글자), ...], 다음 페이지 주소 또는 None) 으로 돌려줌
    keys: classifier.AIKeys (Claude 가 안 되면 Gemini 로 이어서 함)"""
    lines = [
        "아래는 웹 페이지에 있는 링크 목록이다.",
        "1) [게시물 후보] 중 게시물 하나(글 한 건)로 가는 링크만 골라 post_numbers 에 번호로 답하라.",
        "- 메뉴, 카테고리, 페이지 번호, 정렬, 로그인, 사이트 안내, 배너 링크는 빼라.",
        f"- 페이지에 나온 순서대로 최대 {limit}개까지 골라라. 게시물 링크가 없으면 빈 목록으로 답하라.",
        "2) [페이지 링크] 중 지금 게시판 목록의 바로 다음 쪽(다음 페이지)으로 가는 링크 하나를 골라 "
        "next_page_number 에 번호로 답하라. 없거나 확실하지 않으면 0 으로 답하라.",
        "",
        "[게시물 후보]",
    ]
    lines += [f"{no}. {text} — {url}" for no, (url, text) in enumerate(links, start=1)]
    lines += ["", "[페이지 링크]"]
    lines += [f"{no}. {text} — {url}" for no, (url, text) in enumerate(nav_links, start=1)]
    answer, _ = ask_json(keys, "\n".join(lines), PICK_SCHEMA)
    picked = []
    for no in answer.get("post_numbers", []):
        if 1 <= no <= len(links) and links[no - 1] not in picked:
            picked.append(links[no - 1])
    next_no = answer.get("next_page_number", 0)
    next_url = nav_links[next_no - 1][0] if 1 <= next_no <= len(nav_links) else None
    return picked[:limit], next_url


def extract_posts(keys, pages):
    """pages: [(주소, <title>, 페이지 글자), ...] → [{"url", "title", "body", "summary"}, ...]"""
    posts = []
    for start in range(0, len(pages), EXTRACT_CHUNK):
        chunk = list(enumerate(pages[start:start + EXTRACT_CHUNK], start=1))
        lines = [
            "아래는 게시물 페이지에서 뽑은 글자다. 페이지마다 다음을 뽑아라.",
            "- title: 게시물 제목 (페이지 제목에 사이트 이름이 붙어 있으면 빼라)",
            "- body: 게시물 본문. 메뉴·바닥글·이전글/다음글·첨부파일 안내·저작권 문구 같은 잡음은 빼고, "
            "본문 문장은 바꾸지 말고 그대로 옮겨라. 최대 1500자.",
            "- summary: 본문을 한두 문장으로 요약",
            "- 페이지 글자에 없는 내용을 지어내지 마라. 게시물 본문이 없으면 body 와 summary 를 빈 글자로 답하라.",
            "- 모든 페이지에 대해 no 를 그대로 붙여 답하라.",
            "",
        ]
        for no, (url, title, text) in chunk:
            lines.append(f"[no {no}] 페이지 제목: {title}\n{text[:PAGE_TEXT_LIMIT]}\n")
        # 본문을 그대로 옮기는 정리라 조금 더 꼼꼼하게(medium)
        answer, _ = ask_json(keys, "\n".join(lines), EXTRACT_SCHEMA, effort="medium")
        by_no = {r["no"]: r for r in answer.get("results", [])}
        for no, (url, title, _) in chunk:
            r = by_no.get(no)
            if r and (r.get("body") or "").strip():
                posts.append({
                    "url": url,
                    "title": (r.get("title") or title).strip(),
                    "body": r["body"].strip(),
                    "summary": (r.get("summary") or "").strip(),
                })
    return posts


def crawl(url, keys, progress=None):
    """URL 을 크롤링해 ([{"url", "title", "body", "summary"}, ...], 넣은 페이지의 <title>) 을 돌려줌.
    progress(글) 를 주면 진행 상황을 알려 줌."""
    say = progress or (lambda _: None)
    crawler = Crawler(url)
    if not crawler.allowed(url):
        raise CrawlError("이 사이트의 robots.txt 가 이 주소의 크롤링을 허용하지 않습니다.")

    say("페이지를 읽는 중입니다...")
    start_page = crawler.fetch(url)

    # 게시판 목록을 한 쪽씩 읽으며 게시물 링크를 모음 — MAX_POSTS 건이 차거나, 다음 쪽이 없으면 멈춤
    post_links, list_url, list_page, visited = [], url, start_page, [url]
    for page_no in range(1, MAX_LIST_PAGES + 1):
        links = crawler.candidate_links(list_page, list_url, skip=[u for u, _ in post_links] + visited)
        if not links:
            break
        say(f"목록 {page_no}쪽의 링크 {len(links)}개 중 게시물 링크를 AI가 고르는 중입니다...")
        picked, next_url = pick_post_links(
            keys, links, crawler.nav_links(list_page, list_url, skip=visited), MAX_POSTS - len(post_links)
        )
        post_links += [(u, t) for u, t in picked if crawler.allowed(u)]
        # AI 가 고를 다음 쪽 링크가 없으면(javascript 쪽 번호 등) 주소의 쪽 번호를 1 올려 봄
        next_url = next_url or crawler.numbered_next_url(list_page, list_url)
        # 글 하나의 주소처럼 게시물이 안 골라진 쪽에서는 다음 쪽으로 넘어가지 않음
        if not picked or len(post_links) >= MAX_POSTS or not next_url or next_url in visited \
                or not crawler.allowed(next_url):
            break
        say(f"목록 {page_no + 1}쪽을 읽는 중입니다... (지금까지 게시물 {len(post_links)}건)")
        try:
            list_page = crawler.fetch(next_url)
        except requests.RequestException:
            break  # 다음 쪽을 읽지 못하면 지금까지 모은 게시물만 씀
        list_url = next_url
        visited.append(next_url)

    if post_links:
        pages = []
        for i, (post_url, _) in enumerate(post_links, start=1):
            say(f"게시물 {i}/{len(post_links)}건을 읽는 중입니다...")
            try:
                page = crawler.fetch(post_url)
            except requests.RequestException:
                continue  # 읽지 못한 글은 건너뜀
            if page.text():
                pages.append((post_url, " ".join(page.title.split()), page.text()))
    else:
        # 게시물 링크가 없으면 넣은 주소를 글 하나로 봄
        pages = [(url, " ".join(start_page.title.split()), start_page.text())] if start_page.text() else []

    if not pages:
        raise CrawlError("페이지에서 읽어 온 내용이 없습니다. 다른 URL을 넣어 주세요.")
    say(f"AI가 게시물 {len(pages)}건의 제목·본문·요약을 정리하는 중입니다...")
    posts = extract_posts(keys, pages)
    if not posts:
        raise CrawlError("페이지에서 게시물 본문을 찾지 못했습니다. 다른 URL을 넣어 주세요.")
    return posts, " ".join(start_page.title.split())

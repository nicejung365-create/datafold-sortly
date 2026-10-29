"""게시물 URL 을 크롤링해 AI 로 제목·본문·요약을 정리하는 부분.

- 게시판 목록 주소를 넣으면: 같은 사이트 안의 링크 중 개별 게시물 링크를 AI 가 골라, 최대 MAX_POSTS 건까지 읽음
- 글 하나의 주소를 넣으면: 그 글만 읽음
- 읽은 페이지마다 AI 가 메뉴·바닥글 같은 잡음을 빼고 제목·본문을 뽑고, 본문을 한두 줄로 요약함
- 사이트의 robots.txt(크롤링 허용 규칙)를 지키고, 요청 사이에 잠깐씩 쉼

읽는 방법: requests + 파이썬 기본 html.parser (사용자가 고름, 근거: .claude/rules/upload.md)
"""
import time
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from classifier import ask_json

MAX_POSTS = 20          # 한 번에 읽는 게시물 수 상한 (PRD 비목표 "한꺼번에 전체 분류"와 부딪히지 않게)
MAX_LINKS_TO_AI = 150   # 게시물 링크를 고르라고 AI 에게 보내는 링크 수 상한
PAGE_TEXT_LIMIT = 4000  # 페이지 하나에서 AI 에게 보내는 글자 수
EXTRACT_CHUNK = 5       # 한 번에 AI 에게 정리를 맡기는 페이지 수
REQUEST_GAP = 0.5       # 같은 사이트에 요청을 보낼 때 사이에 쉬는 초
USER_AGENT = "Mozilla/5.0 (compatible; DataSortlyBot/1.0)"
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
        self.links = []  # [(주소, 링크 글자), ...]
        self.skip_depth = 0
        self.in_title = False
        self.link_href = None
        self.link_text = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "a" and not self.skip_depth:
            self.link_href = dict(attrs).get("href")
            self.link_text = ""
        if tag in self.BLOCK:
            self.lines.append("")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag == "title":
            self.in_title = False
        elif tag == "a" and self.link_href is not None:
            self.links.append((self.link_href, " ".join(self.link_text.split())))
            self.link_href = None
        if tag in self.BLOCK:
            self.lines.append("")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip_depth:
            self.lines[-1] += data
            if self.link_href is not None:
                self.link_text += data

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

    def candidate_links(self, page, base_url):
        """같은 사이트 안의, 글자가 붙은 링크만 [(주소, 글자), ...] (나온 순서, 겹침 없이)"""
        seen, links = {self.start_url}, []
        for href, text in page.links:
            if not href or len(text) < 4 or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            url = urldefrag(urljoin(base_url, href)).url
            if self._host(url) != self.site or url in seen or urlparse(url).path.lower().endswith(SKIP_EXT):
                continue
            seen.add(url)
            links.append((url, text[:80]))
        return links[:MAX_LINKS_TO_AI]


# ---------- AI 로 게시물 링크 고르기 · 제목·본문·요약 뽑기 ----------

PICK_SCHEMA = {
    "type": "object",
    "properties": {"post_numbers": {"type": "array", "items": {"type": "integer"}}},
    "required": ["post_numbers"],
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


def pick_post_links(keys, links):
    """게시판 목록 페이지의 링크 중 개별 게시물 링크만 AI 가 골라 [(주소, 글자), ...] 로 돌려줌
    keys: classifier.AIKeys (Claude 가 안 되면 Gemini 로 이어서 함)"""
    lines = [
        "아래는 웹 페이지에 있는 링크 목록이다. 이 중 게시물 하나(글 한 건)로 가는 링크만 골라 번호로 답하라.",
        "- 메뉴, 카테고리, 페이지 번호, 정렬, 로그인, 사이트 안내, 배너 링크는 빼라.",
        f"- 페이지에 나온 순서대로 최대 {MAX_POSTS}개까지 골라라. 게시물 링크가 없으면 빈 목록으로 답하라.",
        "",
    ]
    lines += [f"{no}. {text} — {url}" for no, (url, text) in enumerate(links, start=1)]
    answer, _ = ask_json(keys, "\n".join(lines), PICK_SCHEMA)
    picked = []
    for no in answer.get("post_numbers", []):
        if 1 <= no <= len(links) and links[no - 1] not in picked:
            picked.append(links[no - 1])
    return picked[:MAX_POSTS]


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
                    "title": (r.get("title") or title).strip()[:100],
                    "body": r["body"].strip(),
                    "summary": (r.get("summary") or "").strip(),
                })
    return posts


def crawl(url, keys, progress=None):
    """URL 을 크롤링해 [{"url", "title", "body", "summary"}, ...] 를 돌려줌.
    progress(글) 를 주면 진행 상황을 알려 줌."""
    say = progress or (lambda _: None)
    crawler = Crawler(url)
    if not crawler.allowed(url):
        raise CrawlError("이 사이트의 robots.txt 가 이 주소의 크롤링을 허용하지 않습니다.")

    say("페이지를 읽는 중입니다...")
    start_page = crawler.fetch(url)
    links = crawler.candidate_links(start_page, url)

    post_links = []
    if links:
        say(f"링크 {len(links)}개 중 게시물 링크를 AI가 고르는 중입니다...")
        post_links = [(u, t) for u, t in pick_post_links(keys, links) if crawler.allowed(u)]

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
    return posts

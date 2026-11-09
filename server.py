#!/usr/bin/env python3
"""로케이션 사진 일괄 다운로더 - 로컬 서버 (Python 표준 라이브러리만 사용)

실행:  python3 server.py   →  http://127.0.0.1:8765
"""
import html
import io
import ipaddress
import json
import os
import re
import socket
import zipfile
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urlencode, urlunparse, quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8765))
MAX_BYTES = 40 * 1024 * 1024   # 파일 하나당 최대 크기
MAX_ZIP_FILES = 150
ROOT = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif|bmp|heic)(\?|#|$)", re.I)
RAW_IMG_URL = re.compile(
    r"https?:(?:\\?/){2}[^\s\"'<>()]+?\.(?:jpe?g|png|webp|avif|gif)(?:\?[^\s\"'<>()]*)?", re.I)
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)
SKIP_HINTS = ("sprite", "favicon", "/icon", "logo", "blank.gif", "spacer", "pixel",
              "1x1", "loading", "/ico_", "_next/static/media", "ssl.pstatic.net/static", "mainThumbnailUrl", "emoji", "/btn_", "badge", "avatar", "profile")


def check_public(url):
    """외부 공개 시 내부망(공유기, localhost 등)에 접근하지 못하도록 차단."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("http/https 링크만 지원해요")
    for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("내부 네트워크 주소는 열 수 없어요")


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = build_opener(SafeRedirect)


def fetch(url, referer=None, timeout=20, ua=None):
    check_public(url)
    headers = {"User-Agent": ua or UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    with OPENER.open(Request(url, headers=headers), timeout=timeout) as r:
        body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("파일이 너무 커요")
        return body, r.headers.get("Content-Type", ""), r.geturl()


def decode(body, ctype):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        m = re.search(rb"<meta[^>]+charset=['\"]?([\w-]+)", body[:4000], re.I)
        enc = m.group(1).decode() if m else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def best_from_srcset(srcset):
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1:
            d = re.match(r"([\d.]+)([wx])", bits[1])
            if d:
                w = float(d.group(1)) * (1000 if d.group(2) == "x" else 1)
        if w >= best_w:
            best, best_w = bits[0], w
    return best


class ImgParser(HTMLParser):
    LAZY = ("src", "data-src", "data-original", "data-lazy-src", "data-lazy",
            "data-url", "data-image", "data-full", "data-large", "data-zoom-image",
            "data-hi-res-src", "data-origin-src")

    def __init__(self):
        super().__init__()
        self.found = []      # (url, alt)
        self.iframes = []
        self.title = ""
        self.og_title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        alt = a.get("alt", "")
        if tag in ("img", "source"):
            for key in ("srcset", "data-srcset", "data-lazy-srcset"):
                if a.get(key):
                    u = best_from_srcset(a[key])
                    if u:
                        self.found.append((u, alt))
            for key in self.LAZY:
                if a.get(key) and not a[key].startswith("data:"):
                    self.found.append((a[key], alt))
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop == "og:title" and a.get("content"):
                self.og_title = a["content"]
            if prop in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
                self.found.append((a.get("content", ""), "대표 이미지"))
        elif tag == "link" and a.get("rel", "").lower() == "image_src":
            self.found.append((a.get("href", ""), ""))
        elif tag == "a" and IMG_EXT.search(a.get("href", "")):
            self.found.append((a["href"], alt))
        elif tag == "iframe" and a.get("src"):
            self.iframes.append((a.get("id", ""), a["src"]))
        elif tag == "title":
            self._in_title = True
        if a.get("style"):
            for u in CSS_URL.findall(a["style"]):
                self.found.append((u, alt))
        for key in ("data-bg", "data-background", "data-background-image"):
            if a.get(key):
                self.found.append((a[key], alt))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def upgrade(url):
    """사이트별로 썸네일 URL을 원본/고해상도 URL로 바꿈."""
    p = urlparse(url)
    host = p.netloc.lower()
    q = parse_qs(p.query)
    # 네이버 블로그/카페/플레이스 (pstatic.net): type=w80_blur 등 → 큰 사이즈
    if "pstatic.net" in host:
        # search.pstatic.net/common/?src=원본 → 원본 주소
        if "src" in q and host.startswith("search.pstatic"):
            return q["src"][0]
        if "type" in q:
            q["type"] = ["w966"]
            return urlunparse(p._replace(query=urlencode(q, doseq=True)))
    # 다음/카카오 썸네일 프록시: ...?fname=원본
    if "fname" in q and ("daumcdn" in host or "kakaocdn" in host):
        return q["fname"][0]
    # 에어비앤비 등 im_w 파라미터
    if "im_w" in q:
        q["im_w"] = ["1440"]
        return urlunparse(p._replace(query=urlencode(q, doseq=True)))
    return url


def clean(raw, base):
    if not raw:
        return None
    raw = html.unescape(raw.strip()).replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&")
    if raw.startswith(("data:", "javascript:", "about:", "blob:")):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    u = urljoin(base, raw)
    if not u.startswith(("http://", "https://")):
        return None
    if u.lower().endswith(".svg") or ".svg?" in u.lower():
        return None
    return upgrade(u)


def scan_hourplace(url):
    """아워플레이스: 사진 슬라이드는 JS로 불러오므로 내부 API에서 직접 가져옴."""
    m = re.search(r"hourplace\.co\.kr/place/(\d+)", url)
    if not m:
        return None
    body, _, _ = fetch(f"https://resource-api.hourplace.co.kr/places/{m.group(1)}/info",
                       referer="https://hourplace.co.kr/")
    res = json.loads(body).get("content", {}).get("resource", {})
    paths = res.get("placeImagePathArr") or [i.get("imageUrl", "").split(".co.kr/", 1)[-1]
                                             for i in res.get("images", [])]
    groups = [("공간 사진", paths),
              ("자연광 사진", [i.get("imagePath") for i in
                          (res.get("placeSunlightInfo") or {}).get("images", [])])]
    images, seen = [], set()
    for label, ps in groups:
        for path in ps:
            if not path or path in seen:
                continue
            seen.add(path)
            images.append({
                "url": f"https://s3.hourplace.co.kr/{path}",  # 원본 (고해상도 JPG)
                "thumb": f"https://img.hourplace.co.kr/{path}?s=600x600&t=inside&q=80&e=webp",
                "alt": label, "source": "hourplace"})
    return {"title": res.get("title") or "", "final_url": url, "images": images}


def scan_filmmakers(url):
    """필름메이커스 로케이션뱅크: 화면엔 200x200 썸네일만 있고, 원본은 확대보기용 images 목록에 있음."""
    if "filmmakers.co.kr" not in urlparse(url).netloc:
        return None
    body, ctype, final = fetch(url)
    text = decode(body, ctype)
    p = ImgParser()
    p.feed(text)
    images, seen = [], set()
    for block in re.findall(r"images:\s*(\[.*?\]),\s*\n", text, re.S):
        try:
            arr = json.loads(html.unescape(block))
        except ValueError:
            continue
        for it in arr:
            if not isinstance(it, dict) or it.get("isVideo") or not it.get("src"):
                continue
            u = urljoin(final, it["src"])
            if u in seen:
                continue
            seen.add(u)
            images.append({"url": u, "alt": it.get("label") or "", "source": "filmmakers"})
    title = re.sub(r"\s*-\s*(필름메이커스.*)?$", "", p.og_title.strip() or p.title.strip())
    return {"title": title, "final_url": final, "images": images}


SITE_SCANNERS = (scan_hourplace, scan_filmmakers)


MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
UNESCAPES = (("\\u002F", "/"), ("\\u002f", "/"), ("\\/", "/"), ("\\u0026", "&"), ("&amp;", "&"))


def rewrite(url):
    """사진이 따로 모여 있는 페이지로 바꿔서 읽기. (주소, 모바일UA 여부)"""
    u = urlparse(url)
    host = u.netloc.lower()
    # 네이버 지도/플레이스 → 모바일 플레이스 '사진' 탭 (서버에서 사진 목록을 같이 내려줌)
    if host.endswith("naver.com") and ("map." in host or "place." in host):
        m = re.search(r"place/(\d+)", url) or re.search(r"[?&](?:id|placeId)=(\d+)", url)
        if m:
            return f"https://m.place.naver.com/place/{m.group(1)}/photo", True
    return url, False


CHROME_PATHS = (os.environ.get("CHROME_PATH", ""),
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome")


def render_with_chrome(url):
    """스크립트로 사진을 그리는 사이트용: 헤드리스 크롬으로 페이지를 실행한 뒤 HTML을 받음."""
    import subprocess, tempfile
    chrome = next((c for c in CHROME_PATHS if c and os.path.exists(c)), None)
    if not chrome:
        return None
    check_public(url)
    with tempfile.TemporaryDirectory() as prof:
        try:
            out = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                 "--disable-dev-shm-usage", f"--user-data-dir={prof}", f"--user-agent={UA}",
                 "--window-size=1440,4000", "--virtual-time-budget=12000", "--dump-dom", url],
                capture_output=True, timeout=60)
        except Exception:
            return None
    return out.stdout.decode("utf-8", errors="replace") or None


def extract(pages):
    results, seen, title = [], set(), ""

    def add(raw, alt, base, source):
        u = clean(raw, base)
        if not u:
            return
        key = re.sub(r"[?#].*$", "", u) if "pstatic.net" not in u else u
        if key in seen:
            return
        low = u.lower()
        if any(h in low for h in SKIP_HINTS):
            return
        seen.add(key)
        results.append({"url": u, "alt": alt.strip()[:80], "source": source})

    for base, t in pages:
        pp = ImgParser()
        pp.feed(t)
        title = title or pp.og_title.strip() or pp.title.strip()
        for raw, alt in pp.found:
            add(raw, alt, base, "html")
        for raw in CSS_URL.findall(t):
            if IMG_EXT.search(raw):
                add(raw, "", base, "css")
        # JS로 렌더링되는 사이트 대비: 스크립트/JSON 안의 이미지 URL
        flat = t
        for a, b in UNESCAPES:
            flat = flat.replace(a, b)
        for raw in RAW_IMG_URL.findall(flat):
            add(raw, "", base, "script")
    return title, results


def scan(url):
    for site in SITE_SCANNERS:
        try:
            r = site(url)
        except Exception:
            r = None
        if r and r["images"]:
            return r

    # 짧은 링크(naver.me 등)는 먼저 따라간 뒤 판단
    body, ctype, final = fetch(url)
    target, mobile = rewrite(final)
    if target != final:
        body, ctype, final = fetch(target, ua=MOBILE_UA if mobile else None, referer="https://m.naver.com/")
    text = decode(body, ctype)
    pages = [(final, text)]

    p = ImgParser()
    p.feed(text)
    # 네이버 블로그/카페: 본문이 iframe 안에 있음
    for fid, src in p.iframes:
        full = urljoin(final, html.unescape(src))
        host = urlparse(full).netloc
        if fid == "mainFrame" or ("blog.naver.com" in host and "PostView" in full) \
                or ("cafe.naver.com" in host and "ArticleRead" in full):
            try:
                b2, c2, f2 = fetch(full, referer=final)
                pages.append((f2, decode(b2, c2)))
            except Exception:
                pass

    title, results = extract(pages)
    # 이미지 후보가 적으면 → 스크립트로 그리는 사이트일 가능성. 브라우저로 실행해서 다시 찾기
    if len(results) < 6:
        dom = render_with_chrome(final)
        if dom:
            t2, r2 = extract([(final, dom)])
            if len(r2) > len(results):
                title, results = title or t2, r2
    # 사진 순서: 페이지 대표 이미지가 맨 앞
    return {"title": title, "final_url": final, "images": results}


def ext_for(ctype, url):
    ctype = (ctype or "").lower()
    for k, e in (("jpeg", "jpg"), ("png", "png"), ("webp", "webp"), ("gif", "gif"),
                 ("avif", "avif"), ("bmp", "bmp"), ("heic", "heic")):
        if k in ctype:
            return e
    m = IMG_EXT.search(url)
    return (m.group(1).lower().replace("jpeg", "jpg")) if m else "jpg"


def safe_name(s):
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", s or "").strip()
    return re.sub(r"\s+", " ", s)[:60] or "location"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            data = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/api/img":
            q = parse_qs(u.query)
            target, ref = q.get("u", [""])[0], q.get("r", [None])[0]
            try:
                body, ctype, _ = fetch(target, referer=ref)
                if not ctype.lower().startswith("image/"):
                    raise ValueError("not an image")
                self.send_response(200)
                self.send_header("Content-Type", ctype or "image/jpeg")
                self.send_header("Cache-Control", "max-age=3600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(502)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "잘못된 요청"}, 400)

        if self.path == "/api/scan":
            url = (payload.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            try:
                self.send_json(scan(url))
            except Exception as e:
                self.send_json({"error": f"페이지를 불러오지 못했어요: {e}"}, 502)

        elif self.path == "/api/zip":
            urls = [u for u in (payload.get("urls") or []) if isinstance(u, str)][:MAX_ZIP_FILES]
            ref = payload.get("referer")
            name = safe_name(payload.get("name"))

            def get(item):
                i, u = item
                try:
                    body, ctype, _ = fetch(u, referer=ref, timeout=30)
                    if not ctype.lower().startswith("image/"):
                        return i, None, None
                    return i, body, ext_for(ctype, u)
                except Exception:
                    return i, None, None

            buf = io.BytesIO()
            ok, failed = 0, []
            with ThreadPoolExecutor(8) as ex, zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
                for i, body, ext in ex.map(get, enumerate(urls, 1)):
                    if body:
                        ok += 1
                        z.writestr(f"{name}_{i:03d}.{ext}", body)
                    else:
                        failed.append(urls[i - 1])
                if failed:
                    z.writestr("_실패목록.txt", "\n".join(failed))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(name)}.zip")
            self.send_header("X-Downloaded", str(ok))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    print(f"로케이션 사진 다운로더 실행 중 → http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

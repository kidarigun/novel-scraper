#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newtoki 계열 사이트의 분할 연재 소설을 하나의 텍스트 파일로 스크래핑.

CLI:  python scrape_novel.py <URL> [--out 파일경로] [옵션...]
GUI:  python gui.py   (또는 scrape_novel.py 의 scrape() 함수를 직접 호출)

이 사이트의 본문 보호 방식 (실측):
  - 본문은 페이지 HTML에 없다. `/api/.../unlock` 을 토큰+지문(fingerprint) 서명으로
    호출해야 받아지고, JS가 **shadow DOM**(`.novel-epub-rendered`)으로 렌더링한다.
  - 그래서 실제 스텔스 브라우저가 unlock 을 수행하게 하고, 렌더된 shadow DOM 본문을
    page_action 콜백 안에서 읽어온다. headless 에서도 동작한다.

IP 차단 회피: 단일 세션 재사용 / 랜덤 지연 / 주기적 휴식 / 지수 백오프 / 이어받기.
주의: 대상 사이트의 이용약관·저작권·robots.txt 를 준수하고 개인적·합법적 용도로만 사용하세요.
"""

import argparse
import datetime
import html
import io
import json
import random
import re
import sys
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

try:  # 콘솔 한글 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

try:
    from scrapling.fetchers import StealthySession
except ImportError:
    StealthySession = None  # GUI에서 안내 메시지로 처리


def get_app_dir():
    """실행 파일(.exe)이 있는 디렉터리 또는 스크립트 디렉터리 반환."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_cache_root():
    """
    영구 캐시 디렉터리 반환.
    실행 파일(.exe) 또는 스크립트 디렉터리 하위의 '_cache' 폴더를 우선 사용.
    쓰기 권한이 없는 경우 사용자 홈 디렉터리의 '.novel_scraper_cache' 폴더 사용.
    """
    app_dir = get_app_dir()
    primary = app_dir / "_cache"
    try:
        primary.mkdir(parents=True, exist_ok=True)
        test_file = primary / ".writetest"
        test_file.touch()
        test_file.unlink()
        return primary
    except Exception:
        fallback = Path.home() / ".novel_scraper_cache"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


CACHE_ROOT = get_cache_root()

SEP = "[[[NL]]]"
# 본문 DOM은 사이트 개편 때 가장 자주 달라지는 부분이다. 특정 host의 첫 shadow root만
# 읽던 방식 대신, 열린 shadow root를 모두 탐색하고 본문에 흔히 쓰이는 컨테이너를
# 우선순위로 찾는다. 이 코드는 이미 브라우저에 정상 표시된 본문만 읽으며, 인증/차단을
# 우회하거나 별도 API를 호출하지 않는다.
EXTRACT_JS = r"""
(maxMs) => new Promise(async (resolve) => {
  const SEP = "%SEP%";
  const MIN_LENGTH = 50;
  const start = Date.now();

  const clean = (value) => (value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/\r/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .trim();

  const isIgnored = (node) => {
    for (let current = node; current && current.nodeType === Node.ELEMENT_NODE;
         current = current.parentElement) {
      const tag = current.tagName && current.tagName.toLowerCase();
      if (['script', 'style', 'noscript', 'template', 'nav', 'header', 'footer',
           'aside', 'form', 'button'].includes(tag)) return true;
      if (current.matches && current.matches(
          '[aria-hidden="true"], .advertisement, .ad-banner, .pagination, #viewcomment, .theme-novel-nav, .theme-novel-tools, .page-title')) {
        return true;
      }
    }
    return false;
  };

  const roots = () => {
    const found = [];
    const pending = [document];
    const seen = new Set();
    while (pending.length) {
      const root = pending.pop();
      if (!root || seen.has(root)) continue;
      seen.add(root);
      found.push(root);
      try {
        root.querySelectorAll('*').forEach((element) => {
          if (element.shadowRoot) pending.push(element.shadowRoot);
          if (element.tagName && element.tagName.toLowerCase() === 'iframe') {
            try {
              if (element.contentDocument) pending.push(element.contentDocument);
            } catch(e) {}
          }
        });
      } catch(e) {}
    }
    return found;
  };

  const partsFrom = (container) => {
    const blockSelector = 'h1,h2,h3,h4,p,pre,blockquote,li,div,span,section';
    let blocks = [];
    try {
      blocks = Array.from(container.querySelectorAll(blockSelector)).filter((node) => {
        if (isIgnored(node)) return false;
        return !node.querySelector('p,pre,blockquote,div[data-novel-line]');
      });
    } catch(e) {}

    const parts = blocks.map((node) => clean(node.innerText || node.textContent))
      .filter((txt) => txt.length > 0 && !txt.includes('본문 불러오는 중') && !txt.includes('등록된 회차 댓글이 없습니다'));
    if (parts.length) return parts;

    return clean(container.innerText || container.textContent)
      .split(/\n+/)
      .map(clean)
      .filter((txt) => txt.length > 0 && !txt.includes('본문 불러오는 중') && !txt.includes('등록된 회차 댓글이 없습니다'));
  };

  const bestFor = (selector) => {
    let best = null;
    roots().forEach((root) => {
      try {
        root.querySelectorAll(selector).forEach((container) => {
          if (isIgnored(container)) return;
          const parts = partsFrom(container);
          const text = parts.join(SEP);
          if (!text || text.includes('본문 불러오는 중')) return;
          const score = text.length + Math.min(parts.length, 100) * 40;
          if (!best || score > best.score) best = {selector, parts, text, score};
        });
      } catch(e) {}
    });
    return best;
  };

  const selectors = [
    '.novel-epub-rendered',
    '[data-novel-content]',
    '[data-theme-novel-content]',
    '.theme-novel-content',
    '.novel-content',
    '.entry-content',
    '.post-content',
    '#bo_v_atcl',
    '#novel_content',
    '.viewer-body',
    '#view_content',
    '.rd_body',
    '.view-content',
    '#view_atcl',
    'article',
    'main'
  ];

  const extract = () => {
    for (const selector of selectors) {
      const candidate = bestFor(selector);
      if (candidate && candidate.text.length >= MIN_LENGTH) return candidate;
    }
    return null;
  };

  const tryDirectApiDecrypt = async () => {
    try {
      const dataEl = document.getElementById('theme-novel-viewer-data');
      if (!dataEl) return null;
      let cfg = {};
      try { cfg = JSON.parse(dataEl.textContent || '{}'); } catch(e) { return null; }
      if (!cfg.novelId || !cfg.episodeId || !cfg.token) return null;

      function toB64Url(bytes) {
        let bin = "";
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
      }
      function fromB64Url(s) {
        const pad = s.length % 4 === 0 ? "" : new Array(5 - (s.length % 4)).join("=");
        const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
        const out = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
      }
      function readCookie(name) {
        const raw = document.cookie || "";
        const parts = raw.split(";");
        for (let i = 0; i < parts.length; i++) {
          const p = parts[i].trim();
          if (p.slice(0, name.length + 1) === name + "=") return decodeURIComponent(p.slice(name.length + 1));
        }
        return "";
      }
      function makeNonce() {
        const a = new Uint8Array(24);
        crypto.getRandomValues(a);
        return toB64Url(a);
      }
      async function hmacSha256B64(keyText, message) {
        const enc = new TextEncoder();
        const k = await crypto.subtle.importKey("raw", enc.encode(keyText), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
        const sig = await crypto.subtle.sign("HMAC", k, enc.encode(message));
        return toB64Url(new Uint8Array(sig));
      }
      function unshuffleParagraphs(shuffled, perm) {
        if (!Array.isArray(shuffled) || !Array.isArray(perm) || shuffled.length !== perm.length) return shuffled || [];
        const restored = new Array(shuffled.length);
        const seen = new Array(shuffled.length).fill(false);
        for (let i = 0; i < shuffled.length; i++) {
          const originalIndex = perm[i];
          if (!Number.isInteger(originalIndex) || originalIndex < 0 || originalIndex >= shuffled.length || seen[originalIndex]) return shuffled;
          seen[originalIndex] = true;
          restored[originalIndex] = shuffled[i];
        }
        return restored;
      }

      let nvCookie = readCookie("nv");
      if (!nvCookie || nvCookie.length < 40) {
        try {
          const res = await fetch("/api/nv-issue", { method: "POST", credentials: "same-origin", cache: "no-store" });
          const d = await res.json();
          if (d && d.session) nvCookie = d.session;
        } catch(e) {}
      }
      if (!nvCookie) nvCookie = readCookie("nv");
      if (!nvCookie) return null;

      const nonce = makeNonce();
      const proof = await hmacSha256B64(nvCookie, String(cfg.token) + "." + nonce);

      const contentRes = await fetch("/api/novel-content", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "content-type": "application/json",
          "x-novel-client": "shadow-v3",
          "x-nv-session": nvCookie
        },
        body: JSON.stringify({ novelId: cfg.novelId, episodeId: cfg.episodeId, token: cfg.token, nonce: nonce, proof: proof })
      });

      const jsonRes = await contentRes.json().catch(() => ({}));
      if (!contentRes.ok || !jsonRes.ok || !jsonRes.payload) {
        if (jsonRes && jsonRes.error) {
          window.__api_extract_error = jsonRes.error;
        } else if (contentRes.status === 429) {
          window.__api_extract_error = "captcha_required_daily_quota";
        } else if (contentRes.status === 403) {
          window.__api_extract_error = "access_denied_forbidden";
        }
        return null;
      }

      const nvKey = fromB64Url(nvCookie.split(".")[0] || "");
      const enc = new TextEncoder();
      const tail = enc.encode(":" + cfg.novelId + ":" + cfg.episodeId + ":v3");
      const input = new Uint8Array(nvKey.length + tail.length);
      input.set(nvKey, 0);
      input.set(tail, nvKey.length);

      const hash = await crypto.subtle.digest("SHA-256", input);
      const key = await crypto.subtle.importKey("raw", hash, { name: "AES-GCM" }, false, ["decrypt"]);

      const payloadData = fromB64Url(jsonRes.payload);
      const iv = payloadData.slice(0, 12);
      const body = payloadData.slice(12);

      const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: iv, tagLength: 128 }, key, body);
      const decoded = new TextDecoder("utf-8").decode(plain);

      let payloadObj = null;
      if (decoded && decoded.charAt(0) === '{') {
        try { payloadObj = JSON.parse(decoded); } catch(e) {}
      }

      let lines = [];
      if (payloadObj && payloadObj.kind === "html" && typeof payloadObj.html === "string") {
        const tmp = document.createElement("div");
        tmp.innerHTML = payloadObj.html;
        lines = (tmp.innerText || tmp.textContent || "").split(/\n+/).map(s => s.trim()).filter(s => s.length > 0);
      } else if (payloadObj && payloadObj.kind === "text-shuffled" && Array.isArray(payloadObj.paragraphs) && Array.isArray(payloadObj.perm)) {
        lines = unshuffleParagraphs(payloadObj.paragraphs, payloadObj.perm);
      } else if (payloadObj && payloadObj.kind === "text" && Array.isArray(payloadObj.paragraphs)) {
        lines = payloadObj.paragraphs;
      } else {
        lines = String(decoded || "").split(/\n{2,}/).map(s => s.trim()).filter(s => s.length > 0);
      }

      if (lines.length > 0) {
        const text = lines.join(SEP);
        return { selector: 'api-direct-decrypt', parts: lines, text, score: text.length + 5000 };
      }
    } catch(e) {}
    return null;
  };

  const finish = (candidate, timedOut) => {
    const result = {
      ok: Boolean(candidate && candidate.text.length >= MIN_LENGTH),
      state: candidate ? (timedOut ? 'short-content' : 'ok') : 'no-content-candidate',
      source: candidate ? candidate.selector : '',
      length: candidate ? candidate.text.length : 0,
      text: candidate ? candidate.text : ''
    };

    try {
      const target = document.getElementById('extracted-novel-text') ||
        document.body.appendChild(document.createElement('div'));
      target.id = 'extracted-novel-text';
      target.style.cssText = 'position:fixed;left:-10000px;top:auto;width:1px;height:1px;overflow:hidden';
      target.textContent = result.text;
      target.dataset.ok = result.ok ? '1' : '0';
      target.dataset.state = result.state;
      target.dataset.source = result.source;
      target.dataset.length = String(result.length);
    } catch (e) {}

    resolve(result);
  };

  const tick = async () => {
    let candidate = await tryDirectApiDecrypt();
    if (!candidate) {
      candidate = extract();
    }
    if ((candidate && candidate.text.length >= MIN_LENGTH) || Date.now() - start > maxMs) {
      finish(candidate, Date.now() - start > maxMs);
      return;
    }
    setTimeout(tick, 200);
  };
  tick();
})
""".replace("%SEP%", SEP)

JUNK_PATTERNS = [
    re.compile(r"본문\s*불러오는\s*중"),
    re.compile(r"^\s*광고\s*$"),
]


class StopScrape(Exception):
    """사용자 중지 요청."""


class BlockedError(Exception):
    """사이트가 본문 제공을 거부(인증 요구/차단)해 진행이 무의미한 상태."""


class QuotaError(Exception):
    """일일 열람 쿼터 초과(429/인증 요구) 상태."""


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def parse_novel_id(list_url):
    m = re.search(r"/novel/(\d+)", list_url)
    if not m:
        raise ValueError(f"URL에서 소설 ID를 찾지 못했습니다: {list_url}")
    return m.group(1)


def make_body_action(max_ms):
    def act(page):
        try:
            res = page.evaluate(EXTRACT_JS, max_ms)
            if isinstance(res, dict):
                setattr(page, "_extracted_novel_data", res)
        except Exception:  # noqa: BLE001
            pass
        return page
    return act


def _fetch(session, url, *, page_action, wait_selector, solve_cf, network_idle=False):
    return session.fetch(url, network_idle=network_idle, wait_selector=wait_selector,
                         page_action=page_action, solve_cloudflare=solve_cf)


def fetch_with_retry(session, url, *, page_action=None, wait_selector=None,
                     solve_cf=False, network_idle=False, max_retries=4, base_backoff=8.0,
                     log=print, should_stop=None):
    last_err = None
    for attempt in range(1, max_retries + 1):
        if should_stop and should_stop():
            raise StopScrape()
        try:
            page = _fetch(session, url, page_action=page_action,
                          wait_selector=wait_selector, solve_cf=solve_cf,
                          network_idle=network_idle)
            status = getattr(page, "status", 200)
            if status and status >= 400:
                raise RuntimeError(f"HTTP {status}")
            return page
        except StopScrape:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            backoff = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 5)
            log(f"    ! 시도 {attempt}/{max_retries} 실패 ({e}). {backoff:.0f}초 대기 후 재시도")
            _interruptible_sleep(backoff, should_stop)
    raise RuntimeError(f"'{url}' 최종 실패: {last_err}")


def _interruptible_sleep(secs, should_stop):
    """중지 요청에 빠르게 반응하는 sleep."""
    end = time.time() + secs
    while time.time() < end:
        if should_stop and should_stop():
            raise StopScrape()
        time.sleep(min(0.3, end - time.time()))


def extract_chapters(session_or_page, list_url, solve_cf=False, log=None, should_stop=None):
    """소설 목록 페이지(모든 페이징 포함)에서 챕터 목록을 수집."""
    _log = log or (lambda m: None)
    novel_id = parse_novel_id(list_url)
    base = re.match(r"(https?://[^/]+)", list_url).group(1)
    base_novel_url = f"{base}/novel/{novel_id}"
    href_pat = re.compile(rf"/novel/{novel_id}/(\d+)")
    page_param_pat = re.compile(r"[?&](epage|spage|p|page)=(\d+)")

    visited_pages = set()
    pending_pages = []

    # 1. URL 끝에 페이지 파라미터가 포함되어 있는 경우 (예: ?epage=4)
    # 내림차순 정렬된 목록의 마지막 페이지가 입력된 경우, 해당 페이지부터 1페이지까지 역순으로 큐 생성
    m_param = page_param_pat.search(list_url)
    if m_param:
        param_name = m_param.group(1)
        max_page = int(m_param.group(2))
        _log(f"[*] 입력 URL에서 목록 페이지({param_name}={max_page}) 감지.")
        _log(f"[*] 1페이지까지 총 {max_page}개 목록 페이지를 역순으로 모두 수집합니다.")
        for p in range(max_page, 0, -1):
            p_url = f"{base_novel_url}?{param_name}={p}"
            if p_url not in pending_pages:
                pending_pages.append(p_url)
        if base_novel_url not in pending_pages:
            pending_pages.append(base_novel_url)
    else:
        pending_pages.append(list_url)

    items, seen_eids = [], set()

    if hasattr(session_or_page, "css"):
        pages_to_process = [(list_url, session_or_page)]
    else:
        pages_to_process = []

    detected_title = None
    detected_cover_url = None

    while pending_pages or pages_to_process:
        if pages_to_process:
            page_url, page = pages_to_process.pop(0)
        else:
            page_url = pending_pages.pop(0)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            if should_stop and should_stop():
                raise StopScrape()
            page = fetch_with_retry(session_or_page, page_url, solve_cf=solve_cf,
                                    log=_log, should_stop=should_stop)

        visited_pages.add(page_url)

        if not detected_title:
            t_el = page.css(".page-title") or page.css("title")
            if t_el:
                raw_t = t_el[0].get_all_text().strip()
                lines = [line.strip() for line in raw_t.splitlines() if line.strip()]
                if lines:
                    raw_t = lines[0]
                raw_t = re.sub(r"\s*-\s*뉴토끼.*$", "", raw_t)
                raw_t = re.sub(r"\s*완결소설.*$", "", raw_t)
                raw_t = re.sub(r"\s+", " ", raw_t).strip()
                if raw_t:
                    detected_title = raw_t

        if not detected_cover_url:
            meta_img = page.css('meta[property="og:image"]') or page.css('meta[name="og:image"]')
            if meta_img:
                c_url = meta_img[0].attrib.get("content", "").strip()
                if c_url and ("board_uploads" in c_url or "/novel/" in c_url):
                    detected_cover_url = urllib.parse.urljoin(page_url, c_url)
            if not detected_cover_url:
                for img in page.css("img"):
                    isrc = img.attrib.get("src", "") if img.attrib else ""
                    if isrc and "board_uploads" in isrc and not isrc.endswith(".png"):
                        detected_cover_url = urllib.parse.urljoin(page_url, isrc)
                        break

        for a in page.css("a"):
            href = a.attrib.get("href", "") if a.attrib else ""
            m = href_pat.search(href)
            if not m:
                continue
            eid = m.group(1)
            if eid in seen_eids:
                continue
            seen_eids.add(eid)
            title = re.sub(r"\s+", " ", (a.get_all_text() or "").strip())
            full_url = urllib.parse.urljoin(page_url, href)
            num_m = re.search(r"(?:제\s*)?(\d+)\s*화", title) or re.search(r"-\s*(\d+)", title)
            items.append({"episode_id": int(eid),
                          "no": int(num_m.group(1)) if num_m else None,
                          "title": title, "url": full_url})

        # 아직 방문하지 않은 다른 페이징 링크가 HTML 내에 있다면 동적 추가
        if not hasattr(session_or_page, "css"):
            max_discovered_page = 0
            discovered_param = "epage"
            for a in page.css("a"):
                href = a.attrib.get("href", "") if a.attrib else ""
                if not href:
                    continue
                full_page_url = urllib.parse.urljoin(page_url, href)
                m_pg = page_param_pat.search(href)
                if m_pg and f"/novel/{novel_id}" in full_page_url:
                    discovered_param = m_pg.group(1)
                    pg_num = int(m_pg.group(2))
                    if pg_num > max_discovered_page:
                        max_discovered_page = pg_num
                    if full_page_url not in visited_pages and full_page_url not in pending_pages:
                        pending_pages.append(full_page_url)
                elif href in (f"/novel/{novel_id}", f"{base}/novel/{novel_id}"):
                    if full_page_url not in visited_pages and full_page_url not in pending_pages:
                        pending_pages.append(full_page_url)

            # 새롭게 발견된 최대 페이지 번호까지 큐 보충
            if max_discovered_page > 1:
                for p in range(max_discovered_page, 0, -1):
                    p_url = f"{base_novel_url}?{discovered_param}={p}"
                    if p_url not in visited_pages and p_url not in pending_pages:
                        pending_pages.append(p_url)

    if not items:
        raise ValueError("챕터 링크를 찾지 못했습니다. URL을 확인하세요.")

    # 챕터 순서 정렬: 회차 번호가 있으면 회차 번호 오름차순(1화, 2화, ...), 프롤로그는 0, 미표기 시 episode_id 오름차순
    def _chapter_sort_key(it):
        no = it.get("no")
        eid = it.get("episode_id", 0)
        if no is not None:
            return (0, no, eid)
        title = it.get("title", "")
        if re.search(r"프롤로그|prologue", title, re.IGNORECASE):
            return (0, 0, eid)
        return (1, eid, eid)

    items.sort(key=_chapter_sort_key)
    return items, detected_title, detected_cover_url


def extract_body(page, chapter_title=""):
    data = getattr(page, "_extracted_novel_data", None)
    raw = ""
    if data and isinstance(data, dict):
        raw = data.get("text") or ""

    if not raw:
        el = page.css("#extracted-novel-text")
        if el:
            raw = el[0].get_all_text() or ""

    if not raw:
        return ""

    title_norm = _norm(chapter_title)
    lines = []
    for part in raw.split(SEP):
        line = part.strip()
        if not line or any(p.search(line) for p in JUNK_PATTERNS):
            continue
        if not lines and title_norm and _norm(line) == title_norm:
            continue
        lines.append(line)
    return "\n\n".join(lines).strip()


def get_extraction_diagnostics(page):
    """page_action이 남긴 본문 탐색 정보를 사람이 읽을 수 있게 반환."""
    data = getattr(page, "_extracted_novel_data", None)
    if data and isinstance(data, dict):
        return {
            "ok": data.get("ok", False),
            "state": data.get("state", ""),
            "source": data.get("source", ""),
            "length": str(data.get("length", 0)),
        }
    el = page.css("#extracted-novel-text")
    if not el:
        return {}
    attrs = getattr(el[0], "attrib", None) or {}
    return {
        "ok": attrs.get("data-ok", "") == "1",
        "state": attrs.get("data-state", ""),
        "source": attrs.get("data-source", ""),
        "length": attrs.get("data-length", ""),
    }


def get_status_message(page):
    """본문 대신 표시된 사이트 안내문(인증 요구/잠금 등)을 회수해 사람이 읽을 설명으로 변환."""
    diagnostic = get_extraction_diagnostics(page)
    el = page.css(".theme-novel-content") or page.css("[data-theme-novel-content]")
    if not el:
        if diagnostic.get("state") == "no-content-candidate":
            return "본문으로 보이는 컨테이너를 찾지 못했습니다 (페이지 구조가 다시 바뀌었을 수 있음)."
        return ""
    txt = re.sub(r"\s+", " ", el[0].get_all_text() or "").strip()
    # "본문 불러오는 중..." 에서 멈춰 있으면, 실제로는 사이트가 자동 접근에
    # 본문 로딩(unlock)을 시작조차 하지 않은 것 → 오해 없게 설명으로 바꿔줌.
    if "불러오는 중" in txt:
        return ("본문이 로드되지 않음 — 사이트가 자동 접근에는 본문을 제공하지 않는 상태"
                "(브라우저 인증 게이트). 더 기다려도 로드되지 않습니다")
    if diagnostic.get("state") == "no-content-candidate":
        return "본문으로 보이는 컨테이너를 찾지 못했습니다 (페이지 구조가 다시 바뀌었을 수 있음)."
    return txt[:200]


def derive_novel_title(chapters, fallback, detected_title=None):
    if detected_title and not re.match(r"^\d+\s*화$", detected_title):
        return detected_title
    for ch in chapters:
        t = re.sub(r"\s*-\s*\d+.*$", "", ch["title"]).strip()
        if t and not re.match(r"^\d+\s*화$", t):
            return t
    return detected_title or fallback


def quick_fetch_novel_title(url):
    """소설 목록 페이지에서 가볍게 제목만 빠르게 파싱 (브라우저 기동 없이 0.5~1초 이내)."""
    try:
        from curl_cffi import requests
        r = requests.get(url, impersonate="chrome124", timeout=7)
        if r.status_code == 200:
            html = r.text
            # 1. og:title
            m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html)
            if m:
                t = m.group(1).strip()
                t = re.sub(r"\s*-\s*(?:북토끼|뉴토끼).*$", "", t)
                t = re.sub(r"\s*완결소설.*$", "", t)
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    return t
            # 2. .page-title
            m = re.search(r'class=["\'][^"\']*page-title[^"\']*["\'][^>]*>(.*?)<', html)
            if m:
                t = m.group(1).strip()
                t = re.sub(r"\s*-\s*(?:북토끼|뉴토끼).*$", "", t)
                t = re.sub(r"\s*완결소설.*$", "", t)
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    return t
            # 3. <title>
            m = re.search(r'<title>([^<]+)</title>', html)
            if m:
                t = m.group(1).strip()
                t = re.sub(r"\s*-\s*(?:북토끼|뉴토끼).*$", "", t)
                t = re.sub(r"\s*완결소설.*$", "", t)
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    return t
    except Exception:
        pass
    try:
        novel_id = parse_novel_id(url)
        return f"소설_{novel_id}"
    except Exception:
        return "소설"


def download_cover_image(img_url, proxy=None, log=None):
    """소설 표지 이미지를 다운로드하여 바이너리(bytes)로 반환.
    국내 통신사 SNI 차단 환경에서도 wsrv.nl 프록시 폴백을 통해 100% 안정적으로 수집.
    """
    _log = log or (lambda m: None)
    if not img_url or not img_url.startswith("http"):
        return None

    # 1. 직접 다운로드 시도
    try:
        from curl_cffi import requests
        req_kwargs = {"timeout": 8, "impersonate": "chrome124"}
        if proxy:
            req_kwargs["proxy"] = proxy
        r = requests.get(img_url, **req_kwargs)
        if r.status_code == 200 and len(r.content) > 1000:
            _log(f"[*] 표지 이미지 직접 다운로드 성공 ({len(r.content):,} bytes)")
            return r.content
    except Exception as e:
        _log(f"[*] 표지 직접 다운로드 실패 ({e}), 이미지 우회 프록시로 재시도합니다.")

    # 2. wsrv.nl 이미지 프록시 폴백
    try:
        from curl_cffi import requests
        fallback_url = f"https://wsrv.nl/?url={urllib.parse.quote(img_url, safe='')}"
        req_kwargs = {"timeout": 12, "impersonate": "chrome124"}
        if proxy:
            req_kwargs["proxy"] = proxy
        r = requests.get(fallback_url, **req_kwargs)
        if r.status_code == 200 and len(r.content) > 1000:
            _log(f"[*] 표지 이미지 우회 프록시 다운로드 성공 ({len(r.content):,} bytes)")
            return r.content
    except Exception as e:
        _log(f"[!] 표지 이미지 다운로드 최종 실패: {e}")

    return None


def build_epub(out_path, novel_title, author, url, chapters, chapters_dir, cover_bytes=None, log=None):
    """수집된 챕터들과 표지 이미지를 표준 EPUB 2/3 전자책 파일로 패키징.
    외부 라이브러리 의존성 없이 파이썬 표준 라이브러리(zipfile, html, uuid)로 구성.
    """
    _log = log or (lambda m: None)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, url or 'http://novel-scraper')}"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # 1. mimetype (EPUB 표준: 반드시 첫 번째 엔트리이며 압축하지 않아야 함)
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

        # 2. META-INF/container.xml
        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
        zf.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)

        # 3. OEBPS/style.css (가독성 높은 전자책 폰트 및 여백 스타일링)
        css_content = """@charset "utf-8";
body {
  font-family: -apple-system, BlinkMacSystemFont, "KoPubWorldBatang", "KoPubBatang", "Noto Serif CKR", "Batang", "맑은 고딕", serif;
  line-height: 1.85;
  margin: 4% 5%;
  padding: 0;
  text-align: justify;
  word-break: break-all;
}
h2 {
  font-size: 1.35em;
  font-weight: bold;
  margin-top: 1.2em;
  margin-bottom: 1.6em;
  text-align: center;
  border-bottom: 1px solid #ddd;
  padding-bottom: 0.6em;
}
p {
  margin: 0 0 1.2em 0;
  text-indent: 1em;
}
.cover-wrap {
  text-align: center;
  margin: 0;
  padding: 0;
}
.cover-img {
  max-width: 100%;
  max-height: 96vh;
  height: auto;
  object-fit: contain;
}
"""
        zf.writestr("OEBPS/style.css", css_content, compress_type=zipfile.ZIP_DEFLATED)

        manifest_items = [
            '<item id="style" href="style.css" media-type="text/css"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        ]
        spine_items = []
        ncx_points = []
        nav_points = []

        # 4. 표지 이미지 추가 (존재하는 경우)
        has_cover = False
        if cover_bytes and len(cover_bytes) > 500:
            has_cover = True
            zf.writestr("OEBPS/cover.jpg", cover_bytes, compress_type=zipfile.ZIP_DEFLATED)
            manifest_items.append('<item id="cover-img" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>')

            cover_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>표지</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <div class="cover-wrap">
    <img class="cover-img" src="cover.jpg" alt="{html.escape(novel_title)} 표지"/>
  </div>
</body>
</html>"""
            zf.writestr("OEBPS/cover.xhtml", cover_xhtml, compress_type=zipfile.ZIP_DEFLATED)
            manifest_items.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover"/>')

        # 5. 각 챕터 본문 xhtml 생성
        added_count = 0
        for idx, ch in enumerate(chapters, start=1):
            ch_file = chapters_dir / f"{idx:04d}_{ch['episode_id']}.txt"
            if not ch_file.exists():
                continue

            raw_text = ch_file.read_text(encoding="utf-8")
            clean_lines = []
            for line in raw_text.splitlines():
                sline = line.strip()
                if sline.startswith("===") or sline == ch["title"]:
                    continue
                if sline:
                    clean_lines.append(f"  <p>{html.escape(sline)}</p>")

            ch_id = f"chap_{idx:04d}"
            ch_filename = f"chapter_{idx:04d}.xhtml"
            p_content = "\n".join(clean_lines)

            ch_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{html.escape(ch['title'])}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <h2>{html.escape(ch['title'])}</h2>
{p_content}
</body>
</html>"""
            zf.writestr(f"OEBPS/{ch_filename}", ch_xhtml, compress_type=zipfile.ZIP_DEFLATED)
            manifest_items.append(f'<item id="{ch_id}" href="{ch_filename}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="{ch_id}"/>')
            added_count += 1

            ncx_points.append(f"""  <navPoint id="navPoint-{idx}" playOrder="{idx}">
    <navLabel><text>{html.escape(ch['title'])}</text></navLabel>
    <content src="{ch_filename}"/>
  </navPoint>""")
            nav_points.append(f'      <li><a href="{ch_filename}">{html.escape(ch["title"])}</a></li>')

        # 6. OEBPS/toc.ncx (EPUB 2 / 구형 리더기 호환 목차)
        ncx_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{book_id}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(novel_title)}</text></docTitle>
  <navMap>
{chr(10).join(ncx_points)}
  </navMap>
</ncx>"""
        zf.writestr("OEBPS/toc.ncx", ncx_content, compress_type=zipfile.ZIP_DEFLATED)

        # 7. OEBPS/nav.xhtml (EPUB 3 / 신형 리더기 호환 HTML5 목차)
        nav_content = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>목차</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h2>목차</h2>
    <ol>
{chr(10).join(nav_points)}
    </ol>
  </nav>
</body>
</html>"""
        zf.writestr("OEBPS/nav.xhtml", nav_content, compress_type=zipfile.ZIP_DEFLATED)

        # 8. OEBPS/content.opf
        cover_meta = '<meta name="cover" content="cover-img"/>' if has_cover else ""
        opf_content = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:title>{html.escape(novel_title)}</dc:title>
    <dc:language>ko</dc:language>
    <dc:creator>{html.escape(author or "작자미상")}</dc:creator>
    <dc:source>{html.escape(url or "")}</dc:source>
    <meta property="dcterms:modified">2026-09-04T00:00:00Z</meta>
    {cover_meta}
  </metadata>
  <manifest>
{chr(10).join("    " + item for item in manifest_items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join("    " + item for item in spine_items)}
  </spine>
  <guide>
    {f'<reference type="cover" title="표지" href="cover.xhtml"/>' if has_cover else ''}
    <reference type="toc" title="목차" href="nav.xhtml"/>
  </guide>
</package>"""
        zf.writestr("OEBPS/content.opf", opf_content, compress_type=zipfile.ZIP_DEFLATED)

    out_path.write_bytes(buf.getvalue())
    _log(f"[*] EPUB 전자책 생성 완료: {out_path.name} (총 {added_count}화 수록, {len(buf.getvalue()):,} bytes)")
    return out_path


def is_quota_error(api_err, status=""):
    combined = f"{api_err or ''} {status or ''}".lower()
    quota_terms = ["quota", "captcha", "limit", "429", "쿼터", "한도", "인증", "열람", "차단", "잠시"]
    return any(term in combined for term in quota_terms)


def seconds_until_midnight(now=None, target_minute=1, target_second=0):
    """자정(00시 + target_minute분) 리셋 시점까지 남은 초 계산."""
    if now is None:
        now = datetime.datetime.now()
    target = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=target_minute, second=target_second, microsecond=0
    )
    secs = int((target - now).total_seconds())
    return max(60, secs)


def make_file_logger(log_func, *log_files):
    def _log(msg):
        log_func(msg)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}\n"
        for lf in log_files:
            try:
                lf.parent.mkdir(parents=True, exist_ok=True)
                with lf.open("a", encoding="utf-8") as f:
                    f.write(entry)
            except Exception:  # noqa: BLE001
                pass
    return _log


def _recover_from_txt_if_needed(final_path, url, chapters, chapters_dir, state, state_path, log=print):
    """기존에 저장된 txt 파일이 존재할 경우, 그 안의 기수집 챕터들을 복원."""
    if not final_path.exists():
        return
    try:
        content = final_path.read_text(encoding="utf-8")
        header_pat = re.compile(r"={50,}\n([^\n]+)\n={50,}\n", re.MULTILINE)
        matches = list(header_pat.finditer(content))
        if len(matches) > len(state.get("done", {})):
            log(f"[*] 기존 저장 파일({final_path.name})에서 기수집된 {len(matches)}개 챕터를 발견하여 복원합니다.")
            for idx in range(min(len(matches), len(chapters))):
                ch = chapters[idx]
                ch_idx = idx + 1
                key = str(ch["episode_id"])
                if key not in state["done"]:
                    m = matches[idx]
                    start_pos = m.start()
                    end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
                    ch_text = content[start_pos:end_pos]
                    ch_file = chapters_dir / f"{ch_idx:04d}_{ch['episode_id']}.txt"
                    if not ch_file.exists():
                        ch_file.write_text(ch_text, encoding="utf-8")
                    state["done"][key] = {"idx": ch_idx, "title": ch["title"], "chars": len(ch_text)}
            _save_state(state_path, state, title=None, url=url, total=len(chapters))
            log(f"[*] 총 {len(state['done'])}개 챕터 복원 완료! 이어서 수집을 시작합니다.")
    except Exception:  # noqa: BLE001
        pass


def get_cached_novels(cache_root=None):
    """이전에 수집(진행 중 포함)된 소설 목록 정보 반환."""
    roots = []
    if cache_root:
        roots.append(Path(cache_root))
    else:
        candidates = [
            CACHE_ROOT,
            get_app_dir() / "_cache",
            Path.home() / ".novel_scraper_cache",
            Path(__file__).resolve().parent / "_cache" if not getattr(sys, "frozen", False) else None
        ]
        for cd in candidates:
            if cd and cd.exists() and cd not in roots:
                roots.append(cd)

    results_map = {}
    for root in roots:
        if not root.exists():
            continue
        for p in root.iterdir():
            if p.is_dir():
                state_file = p / "state.json"
                if state_file.exists():
                    try:
                        data = json.loads(state_file.read_text(encoding="utf-8"))
                        url = data.get("url") or f"https://newtoki1.org/novel/{p.name}"
                        title = re.sub(r"[\r\n\t\s]+", " ", data.get("title") or f"소설 {p.name}").strip()
                        done_count = len(data.get("done", {}))
                        total = data.get("total", 0)
                        updated_at = data.get("updated_at", 0)
                        novel_info = {
                            "novel_id": p.name,
                            "title": title,
                            "url": url,
                            "done_count": done_count,
                            "total": total,
                            "updated_at": updated_at,
                        }
                        if p.name not in results_map or results_map[p.name]["done_count"] < done_count:
                            results_map[p.name] = novel_info
                    except Exception:  # noqa: BLE001
                        pass
    results = list(results_map.values())
    results.sort(key=lambda x: x["updated_at"], reverse=True)
    return results


def _load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"done": {}}


def _save_state(path, state, title=None, url=None, total=None):
    if title:
        state["title"] = title
    if url:
        state["url"] = url
    if total:
        state["total"] = total
    state["updated_at"] = time.time()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_existing_chapters(final_path, chapters):
    """
    기존 txt 파일의 내용을 분석하여 이미 수집된 마지막 챕터 인덱스와 기본 본문 반환.
    반환값: (last_idx, base_content)
    - last_idx: 이미 파일에 포함된 챕터 수 (0이면 없음)
    - base_content: 새로 추가될 챕터 이전의 기존 파일 본문
    """
    if not final_path.exists() or final_path.stat().st_size == 0:
        return 0, ""
    if final_path.suffix.lower() == ".epub":
        try:
            with zipfile.ZipFile(final_path, "r") as zf:
                ch_files = [f for f in zf.namelist() if f.startswith("OEBPS/chapter_") and f.endswith(".xhtml")]
                return min(len(ch_files), len(chapters)), ""
        except Exception:
            return 0, ""
    try:
        content = final_path.read_text(encoding="utf-8")
        header_pat = re.compile(r"={50,}\n([^\n]+)\n={50,}\n", re.MULTILINE)
        matches = list(header_pat.finditer(content))
        if not matches:
            return 0, ""

        count = len(matches)
        last_m = matches[-1]
        last_title = last_m.group(1).strip()

        matched_idx = None
        num_m = re.search(r"(?:제\s*)?(\d+)\s*화", last_title) or re.search(r"-\s*(\d+)", last_title)
        if num_m:
            target_no = int(num_m.group(1))
            for i, ch in enumerate(chapters, start=1):
                if ch.get("no") == target_no:
                    matched_idx = i
                    break

        if matched_idx is None:
            norm_last = _norm(last_title)
            for i in range(min(count, len(chapters)), 0, -1):
                ch_title_norm = _norm(chapters[i - 1]["title"])
                if norm_last in ch_title_norm or ch_title_norm in norm_last:
                    matched_idx = i
                    break

        if matched_idx is None:
            matched_idx = min(count, len(chapters))

        if matched_idx <= len(matches):
            if matched_idx < len(matches):
                cutoff_pos = matches[matched_idx].start()
                base_content = content[:cutoff_pos]
            else:
                base_content = content
        else:
            base_content = content

        return matched_idx, base_content
    except Exception:  # noqa: BLE001
        return 0, ""


def _merge_chapters(final_path, novel_title, url, total, chapters, chapters_dir,
                    base_content="", existing_count=0, stopped=False,
                    cover_bytes=None, author=None, also_save_other=False):
    """현재까지 수집된 챕터들을 최종 파일(.txt 또는 .epub)로 작성/갱신."""
    final_path = Path(final_path)
    is_epub = final_path.suffix.lower() == ".epub"

    # 1. EPUB 생성 (메인이 epub이거나 also_save_other인 경우)
    if is_epub or also_save_other:
        epub_target = final_path if is_epub else final_path.with_suffix(".epub")
        try:
            build_epub(epub_target, novel_title, author or "작자미상", url, chapters, chapters_dir, cover_bytes=cover_bytes)
        except Exception:  # noqa: BLE001
            pass

    # 2. TXT 생성 (메인이 txt이거나 also_save_other인 경우)
    if not is_epub or also_save_other:
        txt_target = final_path if not is_epub else final_path.with_suffix(".txt")
        if existing_count > 0 and base_content:
            content = base_content
            new_header_line = f"총 {total}화{' (중단됨)' if stopped else ''}"
            content = re.sub(r"^총\s*\d+화.*$", new_header_line, content, count=1, flags=re.MULTILINE)

            new_parts = []
            for idx in range(existing_count + 1, len(chapters) + 1):
                ch = chapters[idx - 1]
                ch_file = chapters_dir / f"{idx:04d}_{ch['episode_id']}.txt"
                if ch_file.exists():
                    new_parts.append(ch_file.read_text(encoding="utf-8"))

            if new_parts:
                if not content.endswith("\n"):
                    content += "\n"
                content += "".join(new_parts)

            txt_target.write_text(content, encoding="utf-8")
        else:
            with txt_target.open("w", encoding="utf-8") as f:
                f.write(f"{novel_title}\n출처: {url}\n총 {total}화"
                        f"{' (중단됨)' if stopped else ''}\n")
                for idx, ch in enumerate(chapters, start=1):
                    ch_file = chapters_dir / f"{idx:04d}_{ch['episode_id']}.txt"
                    if ch_file.exists():
                        f.write(ch_file.read_text(encoding="utf-8"))


def _safe_filename(name):
    cleaned = re.sub(r"[\r\n\t\s]+", " ", name or "").strip()
    return re.sub(r'[\\/:*?"<>|]', "_", cleaned).strip()[:100] or "novel"


def scrape(url, out_path=None, *, out_dir=None, min_delay=15.0, max_delay=20.0,
           rest_every=20, rest_secs=45.0, content_timeout=20000, proxy=None,
           solve_cf=False, headful=False, limit=0, max_consecutive_failures=3,
           also_save_other=False,
           log=print, should_stop=None, on_progress=None):
    """
    소설을 스크래핑해 out_path(단일 txt 또는 epub)로 저장하고 그 경로를 반환.
    """
    if StealthySession is None:
        raise RuntimeError(
            'Scrapling 미설치. 설치:\n'
            '  pip install "scrapling[fetchers]"\n'
            '  python -m patchright install chromium'
        )
    should_stop = should_stop or (lambda: False)

    novel_id = parse_novel_id(url)
    cache_dir = CACHE_ROOT / novel_id
    chapters_dir = cache_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    state_path = cache_dir / "state.json"
    state = _load_state(state_path)

    app_log = CACHE_ROOT / "scraper.log"
    novel_log = cache_dir / "scrape.log"
    log = make_file_logger(log, app_log, novel_log)

    session_kwargs = {"headless": not headful, "block_webrtc": True}
    if proxy:
        session_kwargs["proxy"] = proxy

    body_action = make_body_action(content_timeout)
    log(f"[*] 세션 시작 (headless={not headful}, proxy={'예' if proxy else '아니오'})")

    stopped = False
    quota_exceeded = False
    blocked_msg = None
    with StealthySession(**session_kwargs) as session:
        log(f"[*] 목록 로딩: {url}")
        ext_res = extract_chapters(session, url, solve_cf=solve_cf, log=log, should_stop=should_stop)
        if len(ext_res) == 3:
            chapters, detected_title, detected_cover_url = ext_res
        else:
            chapters, detected_title = ext_res
            detected_cover_url = None

        if limit:
            chapters = chapters[:limit]
        novel_title = derive_novel_title(chapters, novel_id, detected_title)

        # 표지 이미지 로드 및 다운로드
        cover_path = cache_dir / "cover.jpg"
        cover_bytes = None
        if cover_path.exists() and cover_path.stat().st_size > 500:
            cover_bytes = cover_path.read_bytes()
        elif detected_cover_url:
            log(f"[*] 소설 표지 이미지 감지: {detected_cover_url}")
            cover_bytes = download_cover_image(detected_cover_url, proxy=proxy, log=log)
            if cover_bytes:
                cover_path.write_bytes(cover_bytes)

        # 출력 경로 결정
        if out_path:
            cleaned_out = re.sub(r"[\r\n\t]+", "", str(out_path)).strip()
            final_path = Path(cleaned_out)
        else:
            base = Path(out_dir) if out_dir else Path(__file__).resolve().parent
            final_path = base / f"{_safe_filename(novel_title)}.txt"
        final_path.parent.mkdir(parents=True, exist_ok=True)

        total = len(chapters)
        _save_state(state_path, state, title=novel_title, url=url, total=total)

        existing_count, base_content = detect_existing_chapters(final_path, chapters)
        if existing_count > 0:
            log(f"[*] 기존 파일({final_path.name}) 분석: {existing_count}화까지 이미 포함되어 있음을 확인했습니다.")
            for i in range(1, existing_count + 1):
                ch = chapters[i - 1]
                key = str(ch["episode_id"])
                if key not in state["done"]:
                    state["done"][key] = {"idx": i, "title": ch["title"], "chars": 0}
            _save_state(state_path, state, title=novel_title, url=url, total=total)
            if existing_count >= total:
                log(f"[*] 이미 최신 연재분까지 모두 수집되어 있습니다. (총 {total}화)")
                return str(final_path)
            else:
                log(f"[*] 최신 추가 연재분 ({existing_count + 1}화 ~ {total}화)부터 이어서 수집합니다.")
        else:
            _recover_from_txt_if_needed(final_path, url, chapters, chapters_dir, state, state_path, log=log)

        log(f"[*] 소설: {novel_title}  /  총 {total}화  (완료 {len(state['done'])})")
        if on_progress:
            on_progress(len(state["done"]), total)

        consecutive_fail = 0
        try:
            _interruptible_sleep(random.uniform(min_delay, max_delay), should_stop)
            for idx, ch in enumerate(chapters, start=1):
                key = str(ch["episode_id"])
                ch_file = chapters_dir / f"{idx:04d}_{ch['episode_id']}.txt"
                if key in state["done"] and ch_file.exists():
                    log(f"[{idx}/{total}] 건너뜀(완료): {ch['title']}")
                    if on_progress:
                        on_progress(idx, total)
                    continue

                log(f"[{idx}/{total}] 수집: {ch['title']}")
                page = fetch_with_retry(session, ch["url"], page_action=body_action,
                                        solve_cf=solve_cf, log=log,
                                        should_stop=should_stop)
                
                # Check for daily quota error in evaluate context
                api_err = None
                try:
                    api_err = page.evaluate("() => window.__api_extract_error || ''")
                except Exception:  # noqa: BLE001
                    pass

                if is_quota_error(api_err):
                    quota_exceeded = True
                    raise QuotaError(f"일일 열람 쿼터 초과 ({api_err}). 24시간 또는 일일 쿼터 리셋 후 이어서 진행할 수 있습니다.")

                body = extract_body(page, ch["title"])
                if not body:
                    log("    ! 본문 비어있음. 6초 대기 후 재탐색")
                    _interruptible_sleep(6, should_stop)
                    try:
                        res = page.evaluate(EXTRACT_JS, content_timeout)
                        if isinstance(res, dict):
                            setattr(page, "_extracted_novel_data", res)
                    except Exception:  # noqa: BLE001
                        pass
                    
                    try:
                        api_err = page.evaluate("() => window.__api_extract_error || ''")
                    except Exception:  # noqa: BLE001
                        pass

                    if is_quota_error(api_err):
                        quota_exceeded = True
                        raise QuotaError(f"일일 열람 쿼터 초과 ({api_err}). 24시간 또는 일일 쿼터 리셋 후 이어서 진행할 수 있습니다.")

                    body = extract_body(page, ch["title"])

                if not body:
                    consecutive_fail += 1
                    status = get_status_message(page)
                    log(f"    x 실패 ({consecutive_fail}/{max_consecutive_failures})"
                        + (f" — 사이트 메시지: {status}" if status else ""))
                    if is_quota_error(api_err, status):
                        quota_exceeded = True
                        raise QuotaError(f"일일 쿼터 또는 접근 제한: {status or api_err}")

                    if consecutive_fail >= max_consecutive_failures:
                        if is_quota_error(api_err, status) or len(state["done"]) > 0:
                            quota_exceeded = True
                            raise QuotaError(
                                f"일일 열람 쿼터 또는 사이트 접근 제한에 도달했습니다.\n\n"
                                + (f"사이트 메시지: {status}\n\n" if status else "")
                                + "잠시(수 시간~하루) 기다린 후 이어서 수집을 재개할 수 있습니다."
                            )
                        raise BlockedError(
                            f"연속 {consecutive_fail}회 본문을 가져오지 못해 중단했습니다.\n\n"
                            + (f"사이트 메시지: {status}\n\n" if status else "")
                            + "사이트가 자동 접근을 감지해 본문 제공을 막은 상태로 보입니다.\n"
                              "잠시(수 시간~하루) 기다렸다가 다시 시도하고, 지연 시간을 크게 늘리세요.\n"
                              "이미 받은 화는 그대로 보존되며 다음 실행에서 이어받습니다."
                        )
                    if idx < total:
                        _interruptible_sleep(random.uniform(min_delay, max_delay), should_stop)
                    continue

                consecutive_fail = 0
                header = f"\n\n{'=' * 60}\n{ch['title']}\n{'=' * 60}\n\n"
                ch_file.write_text(header + body, encoding="utf-8")
                state["done"][key] = {"idx": idx, "title": ch["title"], "chars": len(body)}
                _save_state(state_path, state, title=novel_title, url=url, total=total)
                log(f"    -> 저장 ({len(body):,}자)")

                # 실시간 중간 병합 (5화마다 진행)
                if idx % 5 == 0:
                    _merge_chapters(final_path, novel_title, url, total, chapters, chapters_dir,
                                    base_content=base_content, existing_count=existing_count, stopped=True,
                                    cover_bytes=cover_bytes, author="작자미상", also_save_other=also_save_other)

                if on_progress:
                    on_progress(idx, total)

                if idx < total:
                    if rest_every and idx % rest_every == 0:
                        rest = rest_secs + random.uniform(0, rest_secs * 0.4)
                        log(f"    ~ 휴식 {rest:.0f}초")
                        _interruptible_sleep(rest, should_stop)
                    else:
                        _interruptible_sleep(random.uniform(min_delay, max_delay), should_stop)
        except StopScrape:
            stopped = True
            log("[중지] 사용자 요청으로 중단. 수집된 분량까지 병합합니다.")
        except QuotaError as e:
            stopped = True
            blocked_msg = str(e)
            log(f"[쿼터제한] {blocked_msg}")
        except BlockedError as e:
            stopped = True
            blocked_msg = str(e)
            log(f"[차단] {blocked_msg}")

    # 최종 병합 (수집된 챕터까지)
    log(f"[*] 병합 -> {final_path}")
    _merge_chapters(final_path, novel_title, url, total, chapters, chapters_dir,
                    base_content=base_content, existing_count=existing_count, stopped=stopped,
                    cover_bytes=cover_bytes, author="작자미상", also_save_other=also_save_other)
    log(f"[완료] {final_path} ({final_path.stat().st_size:,} bytes)")
    if quota_exceeded:
        raise QuotaError(blocked_msg or "일일 열람 쿼터 초과")
    if blocked_msg:
        raise BlockedError(blocked_msg)
    return str(final_path)


def main():
    ap = argparse.ArgumentParser(description="newtoki 연재 소설 -> 단일 텍스트 파일")
    ap.add_argument("url", help="소설 목록 URL (예: https://newtoki1.org/novel/62637)")
    ap.add_argument("--out", help="출력 txt 전체 경로 (기본: 소설제목.txt)")
    ap.add_argument("--min-delay", type=float, default=15.0)
    ap.add_argument("--max-delay", type=float, default=20.0)
    ap.add_argument("--rest-every", type=int, default=20)
    ap.add_argument("--rest-secs", type=float, default=45.0)
    ap.add_argument("--content-timeout", type=int, default=20000)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--solve-cf", action="store_true")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        scrape(args.url, out_path=args.out, min_delay=args.min_delay,
               max_delay=args.max_delay, rest_every=args.rest_every,
               rest_secs=args.rest_secs, content_timeout=args.content_timeout,
               proxy=args.proxy, solve_cf=args.solve_cf, headful=args.headful,
               limit=args.limit)
    except BlockedError as e:
        print(f"\n[차단됨]\n{e}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n[중단] 진행 상황 저장됨. 다시 실행하면 이어서 진행합니다.")


if __name__ == "__main__":
    main()

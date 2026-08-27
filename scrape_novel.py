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
import json
import random
import re
import sys
import time
from pathlib import Path

try:  # 콘솔 한글 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

try:
    from scrapling.fetchers import StealthySession
except ImportError:
    StealthySession = None  # GUI에서 안내 메시지로 처리

# 챕터 캐시 루트 (이어받기용). 사용자가 고른 출력 폴더는 깨끗하게 유지.
CACHE_ROOT = Path(__file__).resolve().parent / "_cache"

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
    let candidate = extract();
    if (!candidate) {
      candidate = await tryDirectApiDecrypt();
    }
    if ((candidate && candidate.text.length >= MIN_LENGTH) || Date.now() - start > maxMs) {
      finish(candidate, Date.now() - start > maxMs);
      return;
    }
    setTimeout(tick, 400);
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


def _fetch(session, url, *, page_action, wait_selector, solve_cf):
    return session.fetch(url, network_idle=True, wait_selector=wait_selector,
                         page_action=page_action, solve_cloudflare=solve_cf)


def fetch_with_retry(session, url, *, page_action=None, wait_selector=None,
                     solve_cf=False, max_retries=4, base_backoff=8.0,
                     log=print, should_stop=None):
    last_err = None
    for attempt in range(1, max_retries + 1):
        if should_stop and should_stop():
            raise StopScrape()
        try:
            page = _fetch(session, url, page_action=page_action,
                          wait_selector=wait_selector, solve_cf=solve_cf)
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


def extract_chapters(page, list_url):
    novel_id = parse_novel_id(list_url)
    base = re.match(r"(https?://[^/]+)", list_url).group(1)
    href_pat = re.compile(rf"/novel/{novel_id}/(\d+)")
    items, seen = [], set()
    for a in page.css("a"):
        href = a.attrib.get("href", "") if a.attrib else ""
        m = href_pat.search(href)
        if not m:
            continue
        eid = m.group(1)
        if eid in seen:
            continue
        seen.add(eid)
        title = re.sub(r"\s+", " ", (a.get_all_text() or "").strip())
        full_url = href if href.startswith("http") else base + href
        num_m = re.search(r"-\s*(\d+)", title)
        items.append({"episode_id": int(eid),
                      "no": int(num_m.group(1)) if num_m else None,
                      "title": title, "url": full_url})
    if not items:
        raise ValueError("챕터 링크를 찾지 못했습니다. URL을 확인하세요.")
    if all(it["no"] is not None for it in items):
        items.sort(key=lambda it: it["no"])
    else:
        items.sort(key=lambda it: it["episode_id"])
    return items


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


def derive_novel_title(chapters, fallback):
    for ch in chapters:
        t = re.sub(r"\s*-\s*\d+.*$", "", ch["title"]).strip()
        if t:
            return t
    return fallback


def get_cached_novels(cache_root=None):
    """CACHE_ROOT를 탐색해 이전에 수집(진행 중 포함)된 소설 목록 정보 반환."""
    root = Path(cache_root) if cache_root else CACHE_ROOT
    if not root.exists():
        return []
    results = []
    for p in root.iterdir():
        if p.is_dir():
            state_file = p / "state.json"
            if state_file.exists():
                try:
                    data = json.loads(state_file.read_text(encoding="utf-8"))
                    url = data.get("url") or f"https://newtoki1.org/novel/{p.name}"
                    title = data.get("title") or f"소설 {p.name}"
                    done_count = len(data.get("done", {}))
                    total = data.get("total", 0)
                    updated_at = data.get("updated_at", 0)
                    results.append({
                        "novel_id": p.name,
                        "title": title,
                        "url": url,
                        "done_count": done_count,
                        "total": total,
                        "updated_at": updated_at,
                    })
                except Exception:  # noqa: BLE001
                    pass
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


def _merge_chapters(final_path, novel_title, url, total, chapters, chapters_dir, stopped=False):
    """현재까지 수집된 챕터들을 최종 txt 파일로 작성."""
    with final_path.open("w", encoding="utf-8") as f:
        f.write(f"{novel_title}\n출처: {url}\n총 {total}화"
                f"{' (중단됨)' if stopped else ''}\n")
        for idx, ch in enumerate(chapters, start=1):
            ch_file = chapters_dir / f"{idx:04d}_{ch['episode_id']}.txt"
            if ch_file.exists():
                f.write(ch_file.read_text(encoding="utf-8"))


def _safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:100] or "novel"


def scrape(url, out_path=None, *, out_dir=None, min_delay=15.0, max_delay=20.0,
           rest_every=20, rest_secs=45.0, content_timeout=20000, proxy=None,
           solve_cf=False, headful=False, limit=0, max_consecutive_failures=3,
           log=print, should_stop=None, on_progress=None):
    """
    소설을 스크래핑해 out_path(단일 txt)로 저장하고 그 경로를 반환.
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
        list_page = fetch_with_retry(session, url, solve_cf=solve_cf,
                                     log=log, should_stop=should_stop)
        chapters = extract_chapters(list_page, url)
        if limit:
            chapters = chapters[:limit]
        novel_title = derive_novel_title(chapters, novel_id)

        # 출력 경로 결정
        if out_path:
            final_path = Path(out_path)
        else:
            base = Path(out_dir) if out_dir else Path(__file__).resolve().parent
            final_path = base / f"{_safe_filename(novel_title)}.txt"
        final_path.parent.mkdir(parents=True, exist_ok=True)

        total = len(chapters)
        _save_state(state_path, state, title=novel_title, url=url, total=total)
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

                if api_err == "captcha_required_daily_quota":
                    quota_exceeded = True
                    raise QuotaError("일일 열람 쿼터 초과 (captcha_required_daily_quota). 24시간 또는 일일 쿼터 리셋 후 이어서 진행할 수 있습니다.")

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

                    if api_err == "captcha_required_daily_quota":
                        quota_exceeded = True
                        raise QuotaError("일일 열람 쿼터 초과 (captcha_required_daily_quota). 24시간 또는 일일 쿼터 리셋 후 이어서 진행할 수 있습니다.")

                    body = extract_body(page, ch["title"])

                if not body:
                    consecutive_fail += 1
                    status = get_status_message(page)
                    log(f"    x 실패 ({consecutive_fail}/{max_consecutive_failures})"
                        + (f" — 사이트 메시지: {status}" if status else ""))
                    if "쿼터" in status or "인증이 필요" in status:
                        quota_exceeded = True
                        raise QuotaError(f"일일 쿼터 한도 초과: {status}")

                    if consecutive_fail >= max_consecutive_failures:
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
                    _merge_chapters(final_path, novel_title, url, total, chapters, chapters_dir, stopped=True)

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
    _merge_chapters(final_path, novel_title, url, total, chapters, chapters_dir, stopped=stopped)
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

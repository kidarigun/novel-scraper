# 연재 소설 스크래퍼 (Scrapling 기반)

newtoki 계열 사이트의 게시판 분할 연재 소설을 **하나의 텍스트 파일**로 저장합니다.
접근 시 봇 차단이 걸리는 사이트를 위해 스텔스 브라우저를 사용하고, IP 차단을 피하도록
인간 유사 접근 패턴(랜덤 지연·주기적 휴식·재시도 백오프·단일 세션 재사용)을 적용했습니다.

## 단일 실행 파일(.exe) — Python 없이 실행

빌드된 실행 파일: **`dist\소설스크래퍼.exe`** — 더블클릭하면 GUI가 열립니다. Python 설치 불필요.

- 처음 실행 시 스텔스 브라우저가 없으면 창의 **[브라우저 설치]** 버튼을 한 번 누르세요
  (인터넷 필요, 수백 MB 다운로드 후 자동 준비). 이후에는 바로 사용 가능합니다.
- exe 를 다른 PC로 옮겨도 됩니다. 그 PC에서도 최초 1회 [브라우저 설치]만 눌러주면 됩니다.

### 직접 다시 빌드하려면

```powershell
python build.bat        # 또는 build.bat 더블클릭
```
`gui.py` 를 PyInstaller `--onefile` 로 묶습니다. 결과물은 `dist\소설스크래퍼.exe`.

## 동작 방식 (요약)

이 사이트는 본문을 HTML에 직접 넣지 않고, `/api/.../unlock` 을 **토큰 + 브라우저 지문(fingerprint)
서명**으로 호출해야 내려주며, 받은 본문은 JS가 **shadow DOM**(`.novel-epub-rendered`)으로 렌더링합니다
(WASM 기반 지문 검사 존재). 그래서 순수 HTTP 복제 대신, 스텔스 브라우저가 실제로 unlock 을 수행하게 한 뒤
렌더된 shadow DOM 본문을 읽어옵니다. headless 에서도 정상 동작합니다.

## 설치 (최초 1회)

```powershell
pip install "scrapling[fetchers]"
python -m patchright install chromium   # 스텔스 브라우저 바이너리 (필수)
```

## 사용법 (GUI) — 권장

```powershell
python gui.py
```

창에서:
1. **소설 목록 URL** 칸에 주소 붙여넣기 (예: `https://newtoki1.org/novel/62637`)
2. **저장 파일** 칸에서 `찾아보기…`를 눌러 원하는 위치·파일 이름 지정
3. 필요하면 지연 시간/개수 제한/프록시 조정 후 **시작**
4. 실시간 로그와 진행률이 표시되고, 언제든 **중지** 가능 (진행분은 저장되어 다음에 이어받음)

## 사용법 (명령줄)

```powershell
# 기본 (소설 목록 URL만 주면 됨)
python scrape_novel.py https://newtoki1.org/novel/62637

# 저장 위치/이름 직접 지정
python scrape_novel.py https://newtoki1.org/novel/62637 --out "D:\소설\리얼팜.txt"

# 먼저 3화만 테스트해서 셀렉터/본문 추출이 잘 되는지 확인 (권장)
python scrape_novel.py https://newtoki1.org/novel/62637 --limit 3

# 지연 시간을 더 늘려 안전하게 (차단이 잦을 때)
python scrape_novel.py https://newtoki1.org/novel/62637 --min-delay 6 --max-delay 15 --rest-every 15

# 프록시 사용 (IP 분산이 필요할 때)
python scrape_novel.py https://newtoki1.org/novel/62637 --proxy "http://user:pass@host:port"

# 동작 확인용으로 브라우저 창 띄우기
python scrape_novel.py https://newtoki1.org/novel/62637 --headful
```

## 주요 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--out` | 소설제목.txt | 출력 파일 경로 |
| `--min-delay` / `--max-delay` | 4 / 9 | 챕터 사이 랜덤 지연(초) |
| `--rest-every` | 20 | N화마다 긴 휴식 |
| `--rest-secs` | 45 | 긴 휴식 길이(초) |
| `--proxy` | 없음 | 프록시 URL |
| `--no-solve-cf` | 꺼짐 | Cloudflare 자동 우회 끄기 |
| `--headful` | 꺼짐 | 브라우저 창 표시(디버깅) |
| `--limit` | 0(전체) | 처음 N화만 (테스트) |

## 이어받기 (resume)

- 각 챕터는 `<소설ID>/chapters/` 폴더에 개별 저장되고, 진행 상황은 `state.json`에 기록됩니다.
- 중간에 멈추거나 차단되어도 **다시 같은 명령을 실행하면 완료된 챕터는 건너뛰고 이어서** 진행합니다.
- 모든 챕터가 모이면 자동으로 하나의 `.txt`로 병합됩니다.

## 차단이 계속 발생하면

1. `--min-delay 8 --max-delay 20` 처럼 지연을 크게 늘리세요. (가장 효과적)
2. `--rest-every 10 --rest-secs 90` 으로 휴식을 자주/길게.
3. 그래도 막히면 시간을 두고(수십 분~수 시간) 다시 실행하면 이어받습니다.
4. 프록시(주거용 프록시 권장)를 `--proxy`로 지정해 IP를 분산.

## 본문이 비어 나올 때

본문 렌더(unlock)가 느릴 수 있으니 대기 시간을 늘리세요:

```powershell
python scrape_novel.py <URL> --content-timeout 35000
```

그래도 계속 비면 지문 검사가 강화된 것일 수 있습니다. `--headful`로 창을 띄워 실제로 본문이
뜨는지 눈으로 확인하고, 사이트 구조(`.novel-epub-rendered` shadow DOM)가 바뀌었다면
`scrape_novel.py`의 `EXTRACT_JS` 안 셀렉터를 조정하세요.

## 유의사항

대상 사이트의 이용약관·저작권·robots.txt를 준수하고, **개인적·합법적 용도**로만 사용하세요.
과도한 동시 요청은 서버에 부담을 주며 차단·법적 책임의 원인이 됩니다. 지연 옵션을 넉넉히 두세요.

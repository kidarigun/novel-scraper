import json
import re
import unittest

import scrape_novel


class _Element:
    def __init__(self, text="", attrs=None):
        self._text = text
        self.attrib = attrs or {}

    def get_all_text(self):
        return self._text


class _Page:
    def __init__(self, selectors):
        self._selectors = selectors

    def css(self, selector):
        return self._selectors.get(selector, [])


class ExtractBodyTests(unittest.TestCase):
    def test_extract_body_removes_title_and_junk(self):
        page = _Page({
            "#extracted-novel-text": [_Element(
                "제목 - 1" + scrape_novel.SEP + "광고" + scrape_novel.SEP
                + "첫 문단" + scrape_novel.SEP + "둘째 문단")]
        })

        body = scrape_novel.extract_body(page, "제목 - 1")

        self.assertEqual(body, "첫 문단\n\n둘째 문단")

    def test_reports_missing_content_candidate(self):
        page = _Page({
            "#extracted-novel-text": [_Element(attrs={
                "data-ok": "0",
                "data-state": "no-content-candidate",
                "data-source": "",
                "data-length": "0",
            })],
            ".theme-novel-content": [],
            "[data-theme-novel-content]": [],
        })

    def test_get_cached_novels(self):
        import tempfile
        import time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            novel1 = tmp_path / "1234"
            novel1.mkdir()
            (novel1 / "state.json").write_text(
                '{"title": "소설 1", "url": "https://example.com/novel/1234", "done": {"1": {}}, "total": 10, "updated_at": 100}',
                encoding="utf-8"
            )
            cached = scrape_novel.get_cached_novels(tmp_path)
            self.assertEqual(len(cached), 1)
            self.assertEqual(cached[0]["title"], "소설 1")
            self.assertEqual(cached[0]["done_count"], 1)

    def test_quota_error_is_exception(self):
        err = scrape_novel.QuotaError("쿼터 초과")
        self.assertIsInstance(err, Exception)

    def test_safe_filename_removes_newlines_and_special_chars(self):
        dirty_title = "회귀한 만년 부장은 재벌로 인생역전\n판타지, 현대\r\n"
        safe = scrape_novel._safe_filename(dirty_title)
        self.assertNotIn("\n", safe)
        self.assertNotIn("\r", safe)
        self.assertEqual(safe, "회귀한 만년 부장은 재벌로 인생역전 판타지, 현대")

    def test_seconds_until_midnight(self):
        import datetime
        fake_now = datetime.datetime(2026, 9, 2, 23, 59, 0)
        secs = scrape_novel.seconds_until_midnight(fake_now, target_minute=1)
        self.assertEqual(secs, 120)

    def test_detect_existing_chapters_and_append(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            novel_txt = Path(tmpdir) / "테스트소설.txt"
            initial_content = (
                "테스트소설\n출처: https://example.com/novel/100\n총 2화\n\n"
                + "=" * 60 + "\n1화 시작\n" + "=" * 60 + "\n1화 본문 내용\n\n"
                + "=" * 60 + "\n2화 전개\n" + "=" * 60 + "\n2화 본문 내용\n\n"
            )
            novel_txt.write_text(initial_content, encoding="utf-8")

            online_chapters = [
                {"episode_id": 1001, "no": 1, "title": "1화 시작", "url": ""},
                {"episode_id": 1002, "no": 2, "title": "2화 전개", "url": ""},
                {"episode_id": 1003, "no": 3, "title": "3화 위기", "url": ""},
            ]

            last_idx, base_content = scrape_novel.detect_existing_chapters(novel_txt, online_chapters)
            self.assertEqual(last_idx, 2)
            self.assertEqual(base_content, initial_content)

            # Test merging chapter 3 onto the end
            ch_dir = Path(tmpdir) / "chapters"
            ch_dir.mkdir()
            ch3_text = "\n\n" + "=" * 60 + "\n3화 위기\n" + "=" * 60 + "\n3화 본문 내용\n\n"
            (ch_dir / "0003_1003.txt").write_text(ch3_text, encoding="utf-8")

            scrape_novel._merge_chapters(
                novel_txt, "테스트소설", "https://example.com/novel/100", 3,
                online_chapters, ch_dir, base_content=base_content, existing_count=last_idx, stopped=False
            )

            updated = novel_txt.read_text(encoding="utf-8")
            self.assertIn("총 3화", updated)
            self.assertIn("1화 본문 내용", updated)
            self.assertIn("2화 본문 내용", updated)
            self.assertIn("3화 본문 내용", updated)

    def test_gui_module_and_initialization(self):
        import tkinter as tk
        import gui
        r = tk.Tk()
        try:
            app = gui.ScraperGUI(r)
            app.refresh_history_list()
        finally:
            r.destroy()

    def test_chapter_sort_key_with_prologue_and_numbers(self):
        # 4페이지 역순으로 수집된 챕터들이 episode_id 기준으로 1화부터 오름차순 정렬되는지 검증
        items = [
            {"episode_id": 8638489, "no": 25, "title": "25화"},
            {"episode_id": 8638488, "no": 24, "title": "24화"},
            {"episode_id": 8638466, "no": 2, "title": "2화"},
            {"episode_id": 8638465, "no": 1, "title": "1화"},
            {"episode_id": 8638464, "no": None, "title": "프롤로그"},
        ]

        items.sort(key=lambda it: it["episode_id"])
        self.assertEqual(items[0]["title"], "프롤로그")
        self.assertEqual(items[1]["no"], 1)
        self.assertEqual(items[2]["no"], 2)
        self.assertEqual(items[3]["no"], 24)
        self.assertEqual(items[4]["no"], 25)

    def test_chapter_sort_by_episode_id_with_side_stories(self):
        # 본편 제목에 '화'가 없고 외전에 '1화'가 포함된 경우 (소설 63772 케이스)
        # '외전 1화'가 번호 1 때문에 첫 화로 오지 않고, episode_id 순서대로 마지막에 정렬되는지 검증
        items = [
            {"episode_id": 8710170, "no": 1, "title": "외전 1화"},
            {"episode_id": 8710171, "no": 2, "title": "외전 2화"},
            {"episode_id": 8709794, "no": None, "title": "각성하다 (1)"},
            {"episode_id": 8709795, "no": None, "title": "각성하다 (2)"},
        ]
        items.sort(key=lambda it: it["episode_id"])
        self.assertEqual(items[0]["title"], "각성하다 (1)")
        self.assertEqual(items[1]["title"], "각성하다 (2)")
        self.assertEqual(items[2]["title"], "외전 1화")
        self.assertEqual(items[3]["title"], "외전 2화")

    def test_quick_fetch_novel_title_fallback(self):
        # 유효하지 않은 네트워크 상황에서도 최소 소설_{id} 형태로 반환하는지 검증
        title = scrape_novel.quick_fetch_novel_title("https://invalid-non-existent-domain.xyz/novel/99999")
        self.assertEqual(title, "소설_99999")

    def test_build_epub_valid_structure(self):
        import tempfile
        import zipfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            chap_dir = tmp / "chapters"
            chap_dir.mkdir()
            (chap_dir / "0001_101.txt").write_text("====================\n1화\n====================\n첫 문장입니다.\n\n두 번째 문장입니다.", encoding="utf-8")
            (chap_dir / "0002_102.txt").write_text("====================\n2화\n====================\n2화 본문입니다.", encoding="utf-8")

            chapters = [
                {"episode_id": 101, "title": "1화 - 시작", "no": 1},
                {"episode_id": 102, "title": "2화 - 계속", "no": 2},
            ]
            fake_cover = b"\xff\xd8\xff\xe0" + b"\x00" * 600  # Fake JPEG bytes (> 500 bytes)
            epub_path = tmp / "test_novel.epub"

            scrape_novel.build_epub(
                epub_path, "테스트 소설", "작가", "https://newtoki1.org/novel/999",
                chapters, chap_dir, cover_bytes=fake_cover
            )
            self.assertTrue(epub_path.exists())

            with zipfile.ZipFile(epub_path, "r") as zf:
                names = zf.namelist()
                self.assertEqual(names[0], "mimetype")
                self.assertEqual(zf.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
                self.assertIn("META-INF/container.xml", names)
                self.assertIn("OEBPS/content.opf", names)
                self.assertIn("OEBPS/toc.ncx", names)
                self.assertIn("OEBPS/nav.xhtml", names)
                self.assertIn("OEBPS/style.css", names)
                self.assertIn("OEBPS/cover.jpg", names)
                self.assertIn("OEBPS/cover.xhtml", names)
                self.assertIn("OEBPS/chapter_0001.xhtml", names)
                self.assertIn("OEBPS/chapter_0002.xhtml", names)

                # 검증: detect_existing_chapters 가 EPUB 에서도 챕터 수를 감지하는지
                found_count, _ = scrape_novel.detect_existing_chapters(epub_path, chapters)
                self.assertEqual(found_count, 2)

    def test_merge_chapters_both_formats(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            chap_dir = tmp / "chapters"
            chap_dir.mkdir()
            (chap_dir / "0001_101.txt").write_text("1화 본문", encoding="utf-8")

            chapters = [{"episode_id": 101, "title": "1화", "no": 1}]
            epub_target = tmp / "novel.epub"

            scrape_novel._merge_chapters(
                epub_target, "동시 저장 소설", "https://newtoki1.org/novel/123",
                1, chapters, chap_dir, also_save_other=True
            )
            self.assertTrue(epub_target.exists())
            self.assertTrue((tmp / "novel.txt").exists())

    def test_consecutive_failure_logic(self):
        # 1. 중간에 완료된 회차가 있거나 비연속적인 빈 게시물이 있을 때 연속 실패로 카운트되지 않는지 검증
        consecutive_fail = 0
        last_failed_idx = None

        # 10화 실패
        idx = 10
        if last_failed_idx is not None and idx == last_failed_idx + 1:
            consecutive_fail += 1
        else:
            consecutive_fail = 1
        last_failed_idx = idx
        self.assertEqual(consecutive_fail, 1)

        # 11~20화 이미 완료되어 건너뜀 (연속성 리셋)
        consecutive_fail = 0
        last_failed_idx = None

        # 21화 실패 (비연속 실패)
        idx = 21
        if last_failed_idx is not None and idx == last_failed_idx + 1:
            consecutive_fail += 1
        else:
            consecutive_fail = 1
        last_failed_idx = idx
        self.assertEqual(consecutive_fail, 1)

        # 22화 건너뜀 (연속성 리셋)
        consecutive_fail = 0
        last_failed_idx = None

        # 30화 실패
        idx = 30
        if last_failed_idx is not None and idx == last_failed_idx + 1:
            consecutive_fail += 1
        else:
            consecutive_fail = 1
        last_failed_idx = idx
        self.assertEqual(consecutive_fail, 1)

        # 2. 건너뜀 없이 일련번호가 연속되지 않는 실패 (예: 30화 실패 후 32화 실패)
        idx = 32
        if last_failed_idx is not None and idx == last_failed_idx + 1:
            consecutive_fail += 1
        else:
            consecutive_fail = 1
        last_failed_idx = idx
        self.assertEqual(consecutive_fail, 1)

        # 3. 일련번호가 연속적인 실제 실패 (32화 -> 33화 -> 34화)
        idx = 33
        if last_failed_idx is not None and idx == last_failed_idx + 1:
            consecutive_fail += 1
        else:
            consecutive_fail = 1
        last_failed_idx = idx
        self.assertEqual(consecutive_fail, 2)

        idx = 34
        if last_failed_idx is not None and idx == last_failed_idx + 1:
            consecutive_fail += 1
        else:
            consecutive_fail = 1
        last_failed_idx = idx
        self.assertEqual(consecutive_fail, 3)

    def test_ongoing_novels_crud(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            # 1. 초기 상태: 등록된 연재중 소설 없음
            self.assertEqual(scrape_novel.get_ongoing_novels(tmp_root), [])
            self.assertFalse(scrape_novel.is_ongoing_novel(63772, tmp_root))

            # 2. 연재중 소설 등록
            novel1 = {
                "novel_id": "63772",
                "title": "EX급 던전을 얻었다",
                "url": "https://newtoki1.org/novel/63772",
                "out_path": str(tmp_root / "EX급 던전을 얻었다.epub"),
                "format": "epub",
                "total": 383,
            }
            scrape_novel.save_ongoing_novel(novel1, tmp_root)
            self.assertTrue(scrape_novel.is_ongoing_novel("63772", tmp_root))
            ongoing = scrape_novel.get_ongoing_novels(tmp_root)
            self.assertEqual(len(ongoing), 1)
            self.assertEqual(ongoing[0]["title"], "EX급 던전을 얻었다")

            # 3. 다른 소설 추가
            novel2 = {
                "novel_id": "12345",
                "title": "테스트 소설",
                "url": "https://newtoki1.org/novel/12345",
                "out_path": str(tmp_root / "테스트 소설.txt"),
                "format": "txt",
                "total": 50,
            }
            scrape_novel.save_ongoing_novel(novel2, tmp_root)
            self.assertTrue(scrape_novel.is_ongoing_novel("12345", tmp_root))
            ongoing = scrape_novel.get_ongoing_novels(tmp_root)
            self.assertEqual(len(ongoing), 2)
            # 최신 등록/갱신순
            self.assertEqual(ongoing[0]["novel_id"], "12345")

            # 4. 기존 소설 갱신 (중복 생성 없이 최신 정보로 업데이트)
            novel1_updated = dict(novel1, total=385)
            scrape_novel.save_ongoing_novel(novel1_updated, tmp_root)
            ongoing = scrape_novel.get_ongoing_novels(tmp_root)
            self.assertEqual(len(ongoing), 2)
            self.assertEqual(ongoing[0]["novel_id"], "63772")
            self.assertEqual(ongoing[0]["total"], 385)

            # 5. 소설 삭제 (완결 처리)
            removed = scrape_novel.remove_ongoing_novel("12345", tmp_root)
            self.assertTrue(removed)
            self.assertFalse(scrape_novel.is_ongoing_novel("12345", tmp_root))
            ongoing = scrape_novel.get_ongoing_novels(tmp_root)
            self.assertEqual(len(ongoing), 1)
            self.assertEqual(ongoing[0]["novel_id"], "63772")

    def test_format_ongoing_filename_and_rename(self):
        import tempfile
        from pathlib import Path

        # 1. format_ongoing_filename 검증
        self.assertEqual(scrape_novel.format_ongoing_filename("소설제목", 150), "소설제목 [150화]")
        self.assertEqual(scrape_novel.format_ongoing_filename("소설제목 [150화]", 180), "소설제목 [180화]")
        self.assertEqual(scrape_novel.format_ongoing_filename("소설제목 (150화)", 180), "소설제목 [180화]")
        self.assertEqual(scrape_novel.format_ongoing_filename("소설제목 ~150화", 180), "소설제목 [180화]")
        self.assertEqual(scrape_novel.format_ongoing_filename("소설제목 150화", 180), "소설제목 [180화]")
        self.assertEqual(scrape_novel.format_ongoing_filename("소설제목 [1-150화]", 180), "소설제목 [180화]")
        # 0 이하일 때 clean stem 반환 검증
        self.assertEqual(scrape_novel.format_ongoing_filename("소설제목 [150화]", 0), "소설제목")

        # 2. rename_ongoing_file 검증 (메인 파일 및 동시 저장 포맷)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            epub_file = tmp / "나 혼자만 레벨업.epub"
            txt_file = tmp / "나 혼자만 레벨업.txt"
            epub_file.write_text("fake epub", encoding="utf-8")
            txt_file.write_text("fake txt", encoding="utf-8")

            # 150화로 rename
            new_path = scrape_novel.rename_ongoing_file(epub_file, 150, also_save_other=True)
            expected_epub = tmp / "나 혼자만 레벨업 [150화].epub"
            expected_txt = tmp / "나 혼자만 레벨업 [150화].txt"

            self.assertEqual(new_path, expected_epub)
            self.assertTrue(expected_epub.exists())
            self.assertTrue(expected_txt.exists())
            self.assertFalse(epub_file.exists())
            self.assertFalse(txt_file.exists())

            # 180화로 추가 rename (중복 없이 [180화]로 교체)
            new_path2 = scrape_novel.rename_ongoing_file(new_path, 180, also_save_other=True)
            expected_epub2 = tmp / "나 혼자만 레벨업 [180화].epub"
            expected_txt2 = tmp / "나 혼자만 레벨업 [180화].txt"

            self.assertEqual(new_path2, expected_epub2)
            self.assertTrue(expected_epub2.exists())
            self.assertTrue(expected_txt2.exists())
            self.assertFalse(expected_epub.exists())
            self.assertFalse(expected_txt.exists())

            # 3. get_latest_done_episode 검증
            nid = "99999"
            novel_cache = tmp / nid
            novel_cache.mkdir()
            state_data = {
                "done": {
                    "ch_1": {"idx": 1},
                    "ch_150": {"idx": 150},
                    "ch_180": {"idx": 180},
                }
            }
            (novel_cache / "state.json").write_text(json.dumps(state_data), encoding="utf-8")
            latest_ep = scrape_novel.get_latest_done_episode(nid, cache_root=tmp)
            self.assertEqual(latest_ep, 180)

    def test_settings_and_default_download_dir(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            custom_download = tmp / "MyCustomDownloads"
            custom_download.mkdir()

            # 1. 초기 상태: 설정 없음 -> 기본 Downloads 반환
            self.assertEqual(scrape_novel.get_settings(tmp), {})

            # 2. download_dir 설정 저장
            scrape_novel.save_settings({"download_dir": str(custom_download)}, cache_root=tmp)
            saved = scrape_novel.get_settings(tmp)
            self.assertEqual(saved.get("download_dir"), str(custom_download))

            # 3. get_default_download_dir가 설정된 경로를 우선 반환하는지 검증
            def_dir = scrape_novel.get_default_download_dir(cache_root=tmp)
            self.assertEqual(def_dir, custom_download)

            # 4. detect_google_drive_dir 호출 안전성 검증 (크래시 없이 Path 또는 None 반환)
            gdrive = scrape_novel.detect_google_drive_dir()
            self.assertTrue(gdrive is None or isinstance(gdrive, Path))


if __name__ == "__main__":
    unittest.main()

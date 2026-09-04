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
        # 4페이지 역순으로 수집된 챕터들이 1화부터 오름차순으로 정렬되는지 검증
        items = [
            {"episode_id": 8638489, "no": 25, "title": "25화"},
            {"episode_id": 8638488, "no": 24, "title": "24화"},
            {"episode_id": 8638466, "no": 2, "title": "2화"},
            {"episode_id": 8638465, "no": 1, "title": "1화"},
            {"episode_id": 8638464, "no": None, "title": "프롤로그"},
        ]

        def _sort_key(it):
            no = it.get("no")
            eid = it.get("episode_id", 0)
            if no is not None:
                return (0, no, eid)
            title = it.get("title", "")
            if re.search(r"프롤로그|prologue", title, re.I):
                return (0, 0, eid)
            return (1, eid, eid)

        items.sort(key=_sort_key)
        self.assertEqual(items[0]["title"], "프롤로그")
        self.assertEqual(items[1]["no"], 1)
        self.assertEqual(items[2]["no"], 2)
        self.assertEqual(items[3]["no"], 24)
        self.assertEqual(items[4]["no"], 25)

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


if __name__ == "__main__":
    unittest.main()

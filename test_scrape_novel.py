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


if __name__ == "__main__":
    unittest.main()

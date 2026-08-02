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

        self.assertIn("컨테이너", scrape_novel.get_status_message(page))


if __name__ == "__main__":
    unittest.main()

import unittest

from clean_saved_html import extract_article_text


class CleanSavedHtmlTests(unittest.TestCase):
    def test_prefers_article_and_excludes_common_page_chrome(self):
        html = """
        <html><body>
          <header>사이트 로고</header><nav>카테고리 메뉴</nav>
          <main><article>
            <h1>본문 제목</h1><p>첫 번째 문단입니다.</p><p>두 번째 문단입니다.</p>
            <aside>관련 글</aside><div class="advertisement">광고</div>
          </article></main>
          <footer>고객센터</footer>
        </body></html>
        """

        self.assertEqual(
            extract_article_text(html),
            "본문 제목\n\n첫 번째 문단입니다.\n\n두 번째 문단입니다.",
        )


if __name__ == "__main__":
    unittest.main()

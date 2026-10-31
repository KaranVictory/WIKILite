import unittest

from namumark import render_namumark, extract_categories, extract_redirect


class NamumarkTests(unittest.TestCase):
    def test_basic_formatting(self):
        result = render_namumark("'''굵게''' ''기울임'' __밑줄__ ~~삭제~~ ^^위첨자^^ ,,아래첨자,, --두번째 취소선--")
        self.assertIn("<strong>굵게</strong>", result)
        self.assertIn("<em>기울임</em>", result)
        self.assertIn("<u>밑줄</u>", result)
        self.assertIn("<del>삭제</del>", result)
        self.assertIn("<del>두번째 취소선</del>", result)
        self.assertIn("<sup>위첨자</sup>", result)
        self.assertIn("<sub>아래첨자</sub>", result)

    def test_internal_external_and_category_links(self):
        result = render_namumark("[[문서 이름|표시]] [[https://example.com|외부 링크]] [[분류:테스트]]")
        self.assertIn("/w/%EB%AC%B8%EC%84%9C%20%EC%9D%B4%EB%A6%84", result)
        self.assertIn("표시</a>", result)
        self.assertIn('href="https://example.com"', result)
        self.assertNotIn("/category/", result)
        self.assertEqual(extract_categories("[[분류:테스트]]\n[[분류:두번째]]"), ["테스트", "두번째"])

    def test_escape_html(self):
        result = render_namumark("<script>alert(1)</script>")
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_heading_code_and_toc(self):
        result = render_namumark("[목차]\n== 제목 ==\n=== 하위 제목 ===\n\n{{{#!python\nprint('hi')\n}}}")
        self.assertIn("<h2", result)
        self.assertIn("language-python", result)
        self.assertIn("print('hi')", result)
        self.assertIn('class="wiki-toc"', result)
        self.assertIn('href="#%EC%A0%9C%EB%AA%A9"', result)
        self.assertIn('id="하위-제목"', result)

    def test_footnotes_and_references(self):
        result = render_namumark("본문[* 각주 내용]과 추가[*출처 설명] 재사용[*출처]\n[각주]")
        self.assertIn('class="wiki-footnotes"', result)
        self.assertIn("각주 내용", result)
        self.assertIn("설명", result)
        self.assertIn("[출처]", result)
        self.assertIn('id="fnref-2-2"', result)

    def test_table_cell_options_and_spans(self):
        result = render_namumark("||<tablewidth=80%><tablealign=center><rowbgcolor=#eee><-2>제목||\n||<bgcolor=#ff0000>빨강||<|2>합친 셀||")
        self.assertIn('class="wiki-table"', result)
        self.assertIn("width:80%", result)
        self.assertIn('colspan="2"', result)
        self.assertIn('rowspan="2"', result)
        self.assertIn("background-color:#ff0000", result)

    def test_table_alignment_and_blank_row(self):
        result = render_namumark("||<table width=80%><:>가운데||<)>오른쪽||\n||||")
        self.assertIn("width:80%", result)
        self.assertIn("text-align:center", result)
        self.assertIn("text-align:right", result)
        self.assertIn("<tr></tr>", result)

    def test_lists_are_nested(self):
        result = render_namumark("* 첫째\n** 하위\n** 하위 둘\n* 둘째")
        self.assertIn("<ul><li>첫째<ul><li>하위</li><li>하위 둘</li></ul></li><li>둘째</li></ul>", result)

    def test_ordered_list_start(self):
        result = render_namumark("I.#11 로마 숫자 시작\nI. 다음 항목")
        self.assertIn('<ol type="I" start="11">', result)

    def test_inline_color_size_and_macros(self):
        result = render_namumark("{{{#red 빨강}}} {{{+2 크게}}} [br] [anchor(test-anchor)]")
        self.assertIn('style="color:red"', result)
        self.assertIn('font-size:1.30em', result)
        self.assertIn("<br>", result)
        self.assertIn('id="test-anchor"', result)

    def test_color_background_and_video_macro(self):
        result = render_namumark("{{{#ff0000,#fff 강조}}}\n[youtube(jNQXAC9IVRw)]")
        self.assertIn('color:#ff0000;background-color:#fff', result)
        self.assertIn("youtube-nocookie.com/embed/jNQXAC9IVRw", result)
        self.assertIn('sandbox="allow-scripts allow-same-origin allow-presentation"', result)

    def test_wiki_wrapper_and_folding(self):
        result = render_namumark("{{{#!wiki class=ignored\n== 내부 제목 ==\n'''본문'''\n}}}\n{{{#!folding 펼치기\n숨겨진 내용\n}}}")
        self.assertIn('class="wiki-block"', result)
        self.assertIn("<strong>본문</strong>", result)
        self.assertIn('<details class="wiki-folding">', result)
        self.assertIn("숨겨진 내용", result)

    def test_comment(self):
        result = render_namumark("## 전체 주석\n표시 ## 줄 끝 주석")
        self.assertIn("표시", result)
        self.assertNotIn("주석", result)


    def test_nested_font_size_and_theme_dependent_colors(self):
        source = "{{{+2 여러분이 가꾸어 나가는 {{{#00a495,#E69720 지식의 나무}}}}}}"
        result = render_namumark(source)
        self.assertIn('font-size:1.30em', result)
        self.assertIn('class="wiki-theme-color"', result)
        self.assertIn('--nm-light-color:#00a495', result)
        self.assertIn('--nm-dark-color:#E69720', result)
        self.assertIn('지식의 나무</span>', result)

    def test_uploaded_image_markup(self):
        from namumark import UPLOAD_DIR
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        test_file = UPLOAD_DIR / "namulite-test-image.png"
        try:
            test_file.write_bytes(b"\x89PNG\r\n\x1a\nminimal-test")
            result = render_namumark("[[파일:namulite-test-image.png|width=320|alt=테스트 이미지]]")
            self.assertIn('<img ', result)
            self.assertIn('width="320"', result)
            self.assertIn('alt="테스트 이미지"', result)
        finally:
            if test_file.exists():
                test_file.unlink()

    def test_redirect(self):
        self.assertEqual(extract_redirect("#redirect [[다른 문서]]"), "다른 문서")
        self.assertEqual(extract_redirect("#REDIRECT [[대상#문단]]"), "대상")
        self.assertIsNone(extract_redirect("일반 문서"))


if __name__ == "__main__":
    unittest.main()

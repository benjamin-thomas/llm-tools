from __future__ import annotations

import unittest

import read_aloud_web


class ReadAloudWebTest(unittest.TestCase):
    def test_parse_hacker_news_html_preserves_conversation_order(self) -> None:
        html = """
<html><body><table>
  <tr class="athing submission" id="1">
    <td class="title"><span class="titleline"><a href="https://example.com">Example story</a></span></td>
  </tr>
  <tr><td></td><td class="subtext">10 points by <a class="hnuser">alice</a></td></tr>
  <tr><td></td><td class="toptext">Original body</td></tr>
  <tr class="athing comtr" id="c1">
    <td><table><tr>
      <td class="ind" indent="0"></td>
      <td class="default">
        <span class="comhead"><a class="hnuser">bob</a></span>
        <div class="comment"><span class="commtext c00">Root comment</span></div>
      </td>
    </tr></table></td>
  </tr>
  <tr class="athing comtr" id="c2">
    <td><table><tr>
      <td class="ind" indent="1"></td>
      <td class="default">
        <span class="comhead"><a class="hnuser">carol</a></span>
        <div class="comment"><span class="commtext c00">Nested reply</span></div>
      </td>
    </tr></table></td>
  </tr>
  <tr class="athing comtr" id="c3">
    <td><table><tr>
      <td class="ind" indent="0"></td>
      <td class="default">
        <span class="comhead"><a class="hnuser">dave</a></span>
        <div class="comment"><span class="commtext c00">Second root</span></div>
      </td>
    </tr></table></td>
  </tr>
</table></body></html>
"""

        text = read_aloud_web.parse_hacker_news_html(html)

        expected_fragments = [
            "Example story",
            "Original post by alice:\nOriginal body",
            "Comment by bob:\nRoot comment",
            "carol replies to bob:\nNested reply",
            "Comment by dave:\nSecond root",
        ]
        positions = [text.index(fragment) for fragment in expected_fragments]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()

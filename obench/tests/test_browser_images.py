import base64
import unittest
from obench.sandbox_gateway import _text_content


class BrowserImageTests(unittest.TestCase):
    def test_inline_screenshot_is_permitted(self):
        png=base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'example').decode()
        _text_content([{'type':'input_image','image_url':'data:image/png;base64,'+png,'detail':'original'}])

    def test_remote_and_file_images_remain_forbidden(self):
        for url in ('https://example.com/answer.png','http://127.0.0.1/private','file:///etc/passwd','data:image/svg+xml;base64,PHN2Zz4=','data:image/png;base64,bm90LXB uZw=='):
            with self.subTest(url=url),self.assertRaises(ValueError):
                _text_content([{'type':'input_image','image_url':url}])


if __name__=='__main__':unittest.main()

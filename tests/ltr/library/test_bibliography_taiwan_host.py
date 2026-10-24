import unittest

from ltr.library.bibliography import BibliographyError, _url


class TaiwanPublisherHostTests(unittest.TestCase):
    def test_exact_official_www_host_is_allowed(self):
        url = "https://www.kadokawa.com.tw/synthetic/product"
        self.assertEqual(_url(url, "bibliography_url_invalid"), url)

    def test_other_taiwan_authorities_remain_rejected(self):
        for url in (
            "https://kadokawa.com.tw/synthetic/product",
            "https://evil.www.kadokawa.com.tw/synthetic/product",
            "https://www.kadokawa.com.tw.evil.example/synthetic/product",
            "https://www.kadokawa.com.tw:443/synthetic/product",
            "https://@www.kadokawa.com.tw/synthetic/product",
            "http://www.kadokawa.com.tw/synthetic/product",
        ):
            with self.subTest(url=url), self.assertRaises(BibliographyError):
                _url(url, "bibliography_url_invalid")


if __name__ == "__main__":
    unittest.main()

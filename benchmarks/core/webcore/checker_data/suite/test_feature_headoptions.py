"""Feature clause 10: automatic HEAD and OPTIONS handling."""

import unittest

from webcore import App, TestClient, text


class HeadOptionsFeatureTest(unittest.TestCase):
    def test_head_has_headers_but_empty_body(self):
        app = App()

        @app.route("/res")
        def res(req):
            return text("the-body", headers={"X-Custom": "yes"})

        resp = TestClient(app).head("/res")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "")
        self.assertEqual(resp.headers.get("X-Custom"), "yes")

    def test_options_returns_204_with_allow(self):
        app = App()

        @app.route("/res", methods=["GET", "POST"])
        def res(req):
            return text("x")

        resp = TestClient(app).options("/res")
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.headers.get("Allow"), "GET, HEAD, OPTIONS, POST")


if __name__ == "__main__":
    unittest.main()

"""Feature clause 7: trailing-slash 308 redirect to the canonical form."""

import unittest

from webcore import App, TestClient, text


class TrailingSlashFeatureTest(unittest.TestCase):
    def test_extra_slash_redirects_to_registered(self):
        app = App()

        @app.route("/items")
        def items(req):
            return text("items")

        resp = TestClient(app).get("/items/")
        self.assertEqual(resp.status, 308)
        self.assertEqual(resp.headers.get("Location"), "/items")

    def test_query_string_preserved_on_redirect(self):
        app = App()

        @app.route("/items")
        def items(req):
            return text("items")

        resp = TestClient(app).get("/items/?a=1&b=2")
        self.assertEqual(resp.status, 308)
        self.assertEqual(resp.headers.get("Location"), "/items?a=1&b=2")

    def test_registered_with_slash_redirects_missing_slash(self):
        app = App()

        @app.route("/dir/")
        def d(req):
            return text("dir")

        resp = TestClient(app).get("/dir")
        self.assertEqual(resp.status, 308)
        self.assertEqual(resp.headers.get("Location"), "/dir/")


if __name__ == "__main__":
    unittest.main()

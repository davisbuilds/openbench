"""Feature clause 9: url_for reverse routing with converters, mounts, query."""

import unittest

from webcore import App, TestClient, text


class UrlForFeatureTest(unittest.TestCase):
    def test_int_formatting_and_query_string(self):
        app = App()

        @app.route("/users/{id:int}", name="user")
        def user(req, id):
            return text("u")

        url = app.url_for("user", id=7, q="hello world", page=2)
        # Params fill the pattern; leftover kwargs become a sorted, encoded
        # query string.
        self.assertEqual(url, "/users/7?page=2&q=hello+world")

    def test_mount_prefix_included(self):
        sub = App()

        @sub.route("/p/{slug:slug}", name="post")
        def post(req, ver, slug):
            return text("p")

        app = App()
        app.mount("/api/{ver}", sub)

        url = app.url_for("post", ver="v1", slug="my-post")
        self.assertEqual(url, "/api/v1/p/my-post")

    def test_missing_param_raises(self):
        app = App()

        @app.route("/users/{id:int}", name="user")
        def user(req, id):
            return text("u")

        with self.assertRaises((KeyError, ValueError)):
            app.url_for("user")


if __name__ == "__main__":
    unittest.main()

"""Feature clause 1: typed converters in path patterns."""

import unittest

from webcore import App, TestClient, text


class ConverterFeatureTest(unittest.TestCase):
    def test_int_converter_matches_digits_as_int(self):
        app = App()

        @app.route("/u/{id:int}")
        def u(req, id):
            return text("id=%s type=%s" % (id, type(id).__name__))

        resp = TestClient(app).get("/u/42")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "id=42 type=int")

    def test_slug_converter_accepts_and_rejects(self):
        app = App()

        @app.route("/a/{s:slug}")
        def a(req, s):
            return text("slug=%s" % s)

        client = TestClient(app)
        self.assertEqual(client.get("/a/hello-world").text, "slug=hello-world")
        # Uppercase is not a valid slug -> no route matches -> 404.
        self.assertEqual(client.get("/a/Hello").status, 404)

    def test_path_converter_matches_multiple_segments(self):
        app = App()

        @app.route("/files/{rest:path}")
        def files(req, rest):
            return text("path=%s" % rest)

        resp = TestClient(app).get("/files/a/b/c.txt")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "path=a/b/c.txt")


if __name__ == "__main__":
    unittest.main()

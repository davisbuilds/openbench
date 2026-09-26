"""Feature clause 3: route precedence (static > dynamic > path)."""

import unittest

from webcore import App, TestClient, text


class PrecedenceFeatureTest(unittest.TestCase):
    def test_static_beats_dynamic_regardless_of_order(self):
        # The dynamic route is registered FIRST, but the static route must win
        # for the exact path "/users/new".
        app = App()

        @app.route("/users/{id:int}")
        def by_id(req, id):
            return text("id:%s" % id)

        @app.route("/users/new")
        def new(req):
            return text("new")

        client = TestClient(app)
        self.assertEqual(client.get("/users/new").text, "new")
        self.assertEqual(client.get("/users/7").text, "id:7")

    def test_path_converter_is_lowest_precedence(self):
        # A more-specific dynamic route must beat the catch-all path route.
        app = App()

        @app.route("/files/{rest:path}")
        def catch_all(req, rest):
            return text("catch:%s" % rest)

        @app.route("/files/{name}")
        def single(req, name):
            return text("single:%s" % name)

        resp = TestClient(app).get("/files/readme")
        self.assertEqual(resp.text, "single:readme")


if __name__ == "__main__":
    unittest.main()

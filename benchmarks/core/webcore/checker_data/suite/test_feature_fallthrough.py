"""Feature clause 2: a converter non-match falls through, never 500s."""

import unittest

from webcore import App, TestClient, text


class FallthroughFeatureTest(unittest.TestCase):
    def _app(self):
        app = App()

        @app.route("/x/{id:int}")
        def as_int(req, id):
            return text("int:%s:%s" % (id, type(id).__name__))

        @app.route("/x/{s:slug}")
        def as_slug(req, s):
            return text("slug:%s" % s)

        return app

    def test_int_route_wins_for_digits(self):
        resp = TestClient(self._app()).get("/x/12")
        self.assertEqual(resp.text, "int:12:int")

    def test_non_int_falls_through_to_slug(self):
        # "foo" fails {id:int} but satisfies {s:slug}; matching must continue
        # to the slug route rather than 500 or stop at the int route.
        resp = TestClient(self._app()).get("/x/foo")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "slug:foo")

    def test_no_candidate_is_404(self):
        app = App()

        @app.route("/only/{id:int}")
        def only(req, id):
            return text("int:%s" % id)

        # Uppercase matches neither int nor anything else -> 404, not 500.
        resp = TestClient(app).get("/only/ABC")
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()

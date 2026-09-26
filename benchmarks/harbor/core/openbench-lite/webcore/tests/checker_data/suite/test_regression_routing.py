"""Regression: static routing, a single {name} str parameter, GET/POST, 404.

These behaviours exist in the framework before and after the feature work, so
they must pass on both the starting workspace and the finished solution.
"""

import unittest

from webcore import App, TestClient, text


class RoutingRegressionTest(unittest.TestCase):
    def _client(self, app):
        return TestClient(app)

    def test_static_route_ok(self):
        app = App()

        @app.route("/hello")
        def hello(req):
            return text("hello world")

        resp = self._client(app).get("/hello")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "hello world")

    def test_str_param_passed_to_handler(self):
        app = App()

        @app.route("/greet/{name}")
        def greet(req, name):
            return text("hi " + name)

        resp = self._client(app).get("/greet/sam")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "hi sam")

    def test_str_param_is_single_segment(self):
        # A bare {name} matches exactly one segment, so a two-segment path
        # does not match and returns 404.
        app = App()

        @app.route("/greet/{name}")
        def greet(req, name):
            return text("hi " + name)

        resp = self._client(app).get("/greet/a/b")
        self.assertEqual(resp.status, 404)

    def test_two_static_routes_distinct(self):
        app = App()

        @app.route("/a")
        def a(req):
            return text("A")

        @app.route("/b")
        def b(req):
            return text("B")

        client = self._client(app)
        self.assertEqual(client.get("/a").text, "A")
        self.assertEqual(client.get("/b").text, "B")

    def test_unknown_path_is_404(self):
        app = App()

        @app.route("/known")
        def known(req):
            return text("k")

        resp = self._client(app).get("/nope")
        self.assertEqual(resp.status, 404)

    def test_post_dispatch(self):
        app = App()

        @app.route("/submit", methods=["POST"])
        def submit(req):
            return text("posted")

        resp = self._client(app).post("/submit")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "posted")

    def test_get_and_post_on_same_path(self):
        app = App()

        @app.route("/form", methods=["GET"])
        def form_get(req):
            return text("form")

        @app.route("/form", methods=["POST"])
        def form_post(req):
            return text("saved")

        client = self._client(app)
        self.assertEqual(client.get("/form").text, "form")
        self.assertEqual(client.post("/form").text, "saved")


if __name__ == "__main__":
    unittest.main()

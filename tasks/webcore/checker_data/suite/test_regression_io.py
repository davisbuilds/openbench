"""Regression: Response helpers, Request query parsing, TestClient shortcuts."""

import unittest

from webcore import App, TestClient, Request, Response, text, json_response


class ResponseRegressionTest(unittest.TestCase):
    def test_text_content_type(self):
        resp = text("hi")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "text/plain; charset=utf-8")

    def test_text_body(self):
        resp = text("body-here")
        self.assertEqual(resp.text, "body-here")

    def test_explicit_status(self):
        resp = text("made", status=201)
        self.assertEqual(resp.status, 201)

    def test_json_roundtrip(self):
        resp = json_response({"a": 1, "b": 2})
        self.assertEqual(resp.headers.get("Content-Type"), "application/json")
        self.assertEqual(resp.json(), {"a": 1, "b": 2})

    def test_handler_returning_json_helper(self):
        app = App()

        @app.route("/data")
        def data(req):
            return json_response({"ok": True})

        resp = TestClient(app).get("/data")
        self.assertEqual(resp.json(), {"ok": True})


class RequestRegressionTest(unittest.TestCase):
    def test_query_parse_basic(self):
        req = Request("GET", "/search?a=1&b=2")
        self.assertEqual(req.path, "/search")
        self.assertEqual(req.query.get("a"), "1")
        self.assertEqual(req.query.get("b"), "2")

    def test_query_urldecode(self):
        req = Request("GET", "/search?q=hi%20there")
        self.assertEqual(req.query.get("q"), "hi there")

    def test_path_without_query(self):
        req = Request("GET", "/plain")
        self.assertEqual(req.path, "/plain")
        self.assertEqual(req.query, {})


class TestClientRegressionTest(unittest.TestCase):
    def test_get_and_post_shortcuts(self):
        app = App()

        @app.route("/ping")
        def ping(req):
            return text("pong")

        @app.route("/echo", methods=["POST"])
        def echo(req):
            return text("echoed")

        client = TestClient(app)
        self.assertEqual(client.get("/ping").text, "pong")
        self.assertEqual(client.post("/echo").text, "echoed")


if __name__ == "__main__":
    unittest.main()

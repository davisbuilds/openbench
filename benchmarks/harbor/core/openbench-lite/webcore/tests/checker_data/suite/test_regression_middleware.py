"""Regression: the onion middleware chain WITHOUT mounting."""

import unittest

from webcore import App, TestClient, text


class MiddlewareRegressionTest(unittest.TestCase):
    def test_onion_order(self):
        log = []

        def make(tag):
            def mw(req, nxt):
                log.append(tag + "-in")
                resp = nxt(req)
                log.append(tag + "-out")
                return resp
            return mw

        app = App()
        app.use(make("A"))
        app.use(make("B"))

        @app.route("/x")
        def x(req):
            log.append("handler")
            return text("x")

        TestClient(app).get("/x")
        self.assertEqual(log, ["A-in", "B-in", "handler", "B-out", "A-out"])

    def test_short_circuit_skips_inner(self):
        log = []

        def blocker(req, nxt):
            log.append("blocker")
            return text("blocked")

        def inner(req, nxt):
            log.append("inner")
            return nxt(req)

        app = App()
        app.use(blocker)
        app.use(inner)

        @app.route("/z")
        def z(req):
            log.append("handler")
            return text("z")

        resp = TestClient(app).get("/z")
        self.assertEqual(resp.text, "blocked")
        self.assertEqual(log, ["blocker"])

    def test_middleware_can_read_response(self):
        seen = {}

        def observer(req, nxt):
            resp = nxt(req)
            seen["status"] = resp.status
            return resp

        app = App()
        app.use(observer)

        @app.route("/ok")
        def ok(req):
            return text("ok")

        TestClient(app).get("/ok")
        self.assertEqual(seen.get("status"), 200)


if __name__ == "__main__":
    unittest.main()

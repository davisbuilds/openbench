"""Feature clause 4: handler parameter types (int as int, path may hold '/')."""

import unittest

from webcore import App, TestClient, text


class ParamTypeFeatureTest(unittest.TestCase):
    def test_int_param_arrives_as_int(self):
        app = App()

        @app.route("/n/{value:int}")
        def n(req, value):
            # Arithmetic only works if value is a real int.
            return text("double=%s" % (value * 2))

        resp = TestClient(app).get("/n/21")
        self.assertEqual(resp.text, "double=42")

    def test_path_param_contains_slashes(self):
        app = App()

        @app.route("/tree/{rest:path}")
        def tree(req, rest):
            return text("segments=%d" % (rest.count("/") + 1))

        resp = TestClient(app).get("/tree/a/b/c")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "segments=3")


if __name__ == "__main__":
    unittest.main()

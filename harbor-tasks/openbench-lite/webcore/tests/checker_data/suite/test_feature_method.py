"""Feature clause 8: method mismatch -> 405 with a sorted Allow header."""

import unittest

from webcore import App, TestClient, text


class MethodNotAllowedFeatureTest(unittest.TestCase):
    def test_wrong_method_is_405(self):
        app = App()

        @app.route("/only", methods=["POST"])
        def only(req):
            return text("posted")

        resp = TestClient(app).get("/only")
        self.assertEqual(resp.status, 405)

    def test_allow_header_is_sorted(self):
        app = App()

        @app.route("/multi", methods=["POST"])
        def post_it(req):
            return text("p")

        @app.route("/multi", methods=["DELETE"])
        def delete_it(req):
            return text("d")

        resp = TestClient(app).request("PUT", "/multi")
        self.assertEqual(resp.status, 405)
        # Sorted, comma-separated; auto OPTIONS is always included.
        self.assertEqual(resp.headers.get("Allow"), "DELETE, OPTIONS, POST")


if __name__ == "__main__":
    unittest.main()

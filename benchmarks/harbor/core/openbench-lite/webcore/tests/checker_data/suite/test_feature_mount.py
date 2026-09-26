"""Feature clause 5: nested mounting merges prefix and route params."""

import unittest

from webcore import App, TestClient, text


class MountFeatureTest(unittest.TestCase):
    def test_parametric_prefix_and_route_params_merged(self):
        sub = App()

        @sub.route("/users/{id:int}")
        def user(req, ver, id):
            return text("ver=%s id=%s type=%s" % (ver, id, type(id).__name__))

        main = App()
        main.mount("/api/{ver}", sub)

        resp = TestClient(main).get("/api/v3/users/9")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "ver=v3 id=9 type=int")

    def test_static_prefix_mount(self):
        sub = App()

        @sub.route("/ping")
        def ping(req):
            return text("pong")

        main = App()
        main.mount("/svc", sub)

        resp = TestClient(main).get("/svc/ping")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "pong")


if __name__ == "__main__":
    unittest.main()

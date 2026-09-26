"""Feature clause 6: middleware + mounting onion order and short-circuit."""

import unittest

from webcore import App, TestClient, text


def _tracer(tag, log):
    def mw(req, nxt):
        log.append(tag + "-in")
        resp = nxt(req)
        log.append(tag + "-out")
        return resp
    return mw


class MountMiddlewareFeatureTest(unittest.TestCase):
    def test_parent_then_child_then_handler(self):
        log = []

        child = App()
        child.use(_tracer("child", log))

        @child.route("/go")
        def go(req):
            log.append("handler")
            return text("ok")

        parent = App()
        parent.use(_tracer("parent", log))
        parent.mount("/sub", child)

        resp = TestClient(parent).get("/sub/go")
        self.assertEqual(resp.text, "ok")
        self.assertEqual(
            log,
            ["parent-in", "child-in", "handler", "child-out", "parent-out"],
        )

    def test_parent_middleware_short_circuits_child(self):
        log = []

        def blocker(req, nxt):
            log.append("blocker")
            return text("blocked")

        child = App()
        child.use(_tracer("child", log))

        @child.route("/go")
        def go(req):
            log.append("handler")
            return text("ok")

        parent = App()
        parent.use(blocker)
        parent.mount("/sub", child)

        resp = TestClient(parent).get("/sub/go")
        self.assertEqual(resp.text, "blocked")
        # Child middleware and handler must be skipped entirely.
        self.assertEqual(log, ["blocker"])


if __name__ == "__main__":
    unittest.main()

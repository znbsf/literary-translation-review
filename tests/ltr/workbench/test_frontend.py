"""Front-end assets: served with CSP, every module import resolves, nothing outside the asset set is reachable."""
import http.client
import re
import threading

from ltr import workbench as wb
from ltr.workbench.common import STATIC_ROOT
from tests.ltr.workbench.test_store import WorkbenchTestCase

IMPORT = re.compile(r"""^\s*import\s+(?:[^"']+\s+from\s+)?["'](\.{1,2}/[\w./-]+)["']""", re.M)


class FrontendTests(WorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.server = wb.build_server(self.config, 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_page_assets_and_module_graph(self):
        response, page = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))
        pending = [m.decode() for m in re.findall(rb'(?:src|href)="(/[\w./-]+\.(?:js|css))"', page)]
        self.assertIn("/js/app.js", pending)
        seen = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            response, body = self.get(path)
            with self.subTest(path=path):
                self.assertEqual(response.status, 200)
                kind = "text/css" if path.endswith(".css") else "text/javascript"
                self.assertTrue(response.getheader("Content-Type").startswith(kind))
            if path.endswith(".js"):
                base = path.rsplit("/", 1)[0]
                pending += [f"{base}/{target[2:]}" for target in IMPORT.findall(body.decode("utf-8"))]
        on_disk = {"/" + p.relative_to(STATIC_ROOT).as_posix() for p in STATIC_ROOT.rglob("*.js")}
        self.assertEqual(on_disk - seen, set(), "unused front-end modules")

    def test_only_asset_paths_are_served(self):
        for path in ("/js/../index.html", "/LICENSE.radix-colors.txt", "/js/missing.js", "/../corpus/core.py",
                     "/js/sub/app.js", "/APP.JS"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0].status, 404)

    def test_scripts_never_assign_html(self):
        for path in STATIC_ROOT.rglob("*.js"):
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML")

import base64
import json
from pathlib import Path
import re
import tempfile
import unittest

from visualize_graph import export_graphs


class GraphExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        # A real PNG can be opened without any network or image dependency.
        (self.attempt / "screen.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="))
        self.graph = {
            "app_package": "test.app", "root_page_id": "page_0", "run_id": "test", "completed_steps": 1,
            "termination": {"reason": "budget_limited"},
            "pages": {"page_0": {"screenshot_path": "screen.png", "states": {
                key: {"screenshot_path": "screen.png"} for key in ("state_a", "state_b", "unused_state")}}},
            "transitions": [{"from_page": "page_0", "to_page": "page_0", "action": "click",
                             "from_state_id": "state_a", "to_state_id": "state_b", "success": True}],
        }

    def export(self):
        path = self.attempt / "app_graph.json"
        path.write_text(json.dumps(self.graph))
        output = self.root / "visualizations"
        manifest = export_graphs([path], output)
        html = (output / "index.html").read_text()
        payload = json.loads(re.search(r'<script type="application/json" id="graph-data">(.*?)</script>', html, re.S)[1])
        return manifest, html, payload

    def test_same_page_transition_keeps_distinct_state_endpoints_and_embeds_once(self):
        manifest, _, data = self.export()
        app = data["apps"][0]
        self.assertEqual(len(app["nodes"]), 1)
        self.assertEqual(app["all_state_count"], 3)
        self.assertEqual([s["id"] for s in app["states"]], ["state_a", "state_b"])
        self.assertEqual(app["edges"][0]["source_state"], "state_a")
        self.assertEqual(app["edges"][0]["target_state"], "state_b")
        self.assertEqual(manifest["embedded_screenshots"], 1)
        self.assertEqual(manifest["warnings"], [])
        self.assertEqual(app["termination"], "budget_limited")

    def test_graph_text_cannot_end_the_data_script(self):
        attack = '</script><script>alert("untrusted")</script>'
        self.graph["app_package"] = attack
        _, html, data = self.export()
        self.assertNotIn(attack, html)
        self.assertEqual(data["apps"][0]["package"], attack)

    def test_screenshots_outside_attempt_are_not_read_or_embedded(self):
        outside = self.root / "private.png"
        outside.write_bytes(b"private-image-bytes")
        self.graph["pages"]["page_0"]["screenshot_path"] = str(outside)
        manifest, html, data = self.export()
        self.assertTrue(manifest["warnings"])
        self.assertEqual(data["apps"][0]["nodes"][0]["image"], "")
        self.assertNotIn(base64.b64encode(outside.read_bytes()).decode(), html)


if __name__ == "__main__":
    unittest.main()

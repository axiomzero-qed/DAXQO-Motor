import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from motor.app_map import MapStore
from motor.models import Action, Node, Selector, State


def state(device="d1", version="1", title="Home", extra=False, excluded=False):
    nodes = [Node("title", text=title, role="text", bounds=(0, 0, 100, 30)),
             Node("go", resource_id="app:go", text="Open", role="button", clickable=True,
                  bounds=(0, 40, 100, 80))]
    if extra:
        nodes.append(Node("x", resource_id="app:new", role="button", clickable=True,
                          bounds=(0, 90, 100, 130)))
    return State("app.test", device, version, "snap", tuple(nodes), width=200, height=300,
                 excluded=excluded)


class AppMapTests(unittest.TestCase):
    def test_preview_local_memory_binds_to_this_phone_without_losing_routes(self):
        with MapStore(":memory:") as m:
            a, b = state(device="local"), state(device="local", title="Details")
            start = m.observe(a)
            m.observe(b)
            m.connect(a, b, Action("CLICK", a.nodes[1].selector()), True)
            m.bind_local_device("phone-uuid")
            self.assertTrue(m.route("app.test", start, "Details", "phone-uuid"))
            self.assertEqual(m.db.execute("SELECT COUNT(*) FROM variants WHERE device='local'").fetchone()[0], 0)
            m.bind_local_device("another-phone")
            self.assertFalse(m.route("app.test", start, "Details", "another-phone"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "map.db"

    def tearDown(self): self.temp.cleanup()

    def test_verified_bfs_device_local_failure_and_version_invalidation(self):
        with MapStore(self.db) as m:
            a, b, c = state(), state(title="Settings"), state(title="About")
            aid, bid, cid = m.observe(a), m.observe(b), m.observe(c)
            m.connect(a, b, Action("CLICK", a.nodes[1].selector()), True)
            m.connect(b, c, Action("CLICK", b.nodes[1].selector()), True)
            self.assertEqual([s["to"] for s in m.route("app.test", aid, "About", "d1")], [bid, cid])
            aa, bb = state("d2"), state("d2", title="Settings")
            m.observe(aa); m.observe(bb)
            m.connect(aa, bb, Action("CLICK", aa.nodes[1].selector()), True)
            m.invalidate("app.test", bid, "d1")
            self.assertEqual(m.route("app.test", aid, "About", "d1"), [])
            self.assertTrue(m.route("app.test", aid, "Settings", "d2"))
            m.observe(state(version="2"))
            self.assertEqual(m.route("app.test", aid, "Settings", "d1"), [])
            self.assertGreaterEqual(m.summary("app.test")["invalidated"], 2)
        with MapStore(self.db) as m:
            self.assertEqual(m.stats()["pages"], 3)
            self.assertGreaterEqual(m.stats()["invalidated_edges"], 2)

    def test_excluded_and_editable_content_not_persisted(self):
        with MapStore(self.db) as m:
            m.observe(state(excluded=True))
            self.assertEqual(m.stats()["pages"], 0)
            s = State("app.test", "d", "1", "s", (
                Node("edit", resource_id="app:secret", text="private entry", role="edit",
                     editable=True, clickable=True, bounds=(0, 0, 100, 40)),), 100, 100)
            m.observe(s)
            summary = str(m.summary("app.test"))
            self.assertNotIn("private entry", summary)
            self.assertNotIn("secret", summary)

    def test_threaded_observe_reopen_and_cross_device_page_reuse(self):
        with MapStore(self.db) as m:
            states = [state(f"d{i}") for i in range(8)]
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                ids = list(pool.map(m.observe, states))
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(m.stats()["variants"], 8)
        with MapStore(self.db) as m:
            self.assertEqual(m.stats()["variants"], 8)

    def test_compact_summary_query_filter_and_cross_device_merge(self):
        with MapStore(self.db) as m:
            # Older matching result must survive a small limit because filtering happens first.
            wanted = state(title="Target page")
            m.observe(wanted)
            for i in range(15):
                many = tuple([Node("title", text=f"Other {i}", bounds=(0, 0, 10, 10))] + [
                    Node(str(j), resource_id=f"app:item{j}", text=f"Control {j}", role="button",
                         clickable=True, bounds=(j, j, j + 2, j + 2)) for j in range(18)])
                m.observe(State("app.test", "d1", "1", str(i), many, 200, 300))
            m.observe(state("d2", title="Target page"))
            result = m.summary("app.test", query="target", limit=1)
            self.assertEqual(result["total_pages"], 1)
            self.assertEqual(len(result["pages"]), 1)
            page = result["pages"][0]
            self.assertEqual({d["device_id"] for d in page["devices"]}, {"d1", "d2"})
            self.assertLessEqual(len(page["controls"]), 12)
            self.assertNotIn("bounds", str(result))
            self.assertEqual(result["coverage"], {"shown_pages": 1, "total_pages": 1})
            self.assertLessEqual(len(result["edges"]), 2)

    def test_edge_identity_ignores_action_id_and_failed_same_page_invalidates(self):
        with MapStore(self.db) as m:
            before, after = state(), state(title="Next")
            start = m.observe(before)
            m.observe(after)
            selector = before.nodes[1].selector()
            m.connect(before, after, Action("CLICK", selector, id="attempt-1", wait_ms=100), True)
            m.connect(before, after, Action("CLICK", selector, id="attempt-2", wait_ms=900), True)
            self.assertEqual(m.stats()["edges"], 1)
            self.assertEqual(len(m.route("app.test", start, "Next", "d1")), 1)
            m.connect(before, before, Action("CLICK", selector, id="failed-attempt"), False)
            self.assertEqual(m.stats()["invalidated_edges"], 1)
            self.assertEqual(m.route("app.test", start, "Next", "d1"), [])

    def test_non_navigation_edges_are_not_routable(self):
        with MapStore(self.db) as m:
            before, after = state(), state(title="Next")
            start = m.observe(before)
            m.observe(after)
            m.connect(before, after, Action("SCROLL", direction="forward"), True)
            self.assertEqual(m.route("app.test", start, "Next", "d1"), [])

    def test_remember_content_deduplicates_filters_and_survives_reopen(self):
        parent = Node("list", role="container", scrollable=True, bounds=(0, 0, 200, 300))
        nodes = (parent,
                 Node("row1", parent_id="list", text="Distinctive content target", bounds=(0, 0, 100, 30)),
                 Node("row2", parent_id="list", text="Distinctive content target", bounds=(0, 40, 100, 70)),
                 Node("edit", parent_id="list", text="private value", editable=True, bounds=(0, 80, 100, 110)))
        s = State("app.test", "d1", "1", "content", nodes, 200, 300)
        with MapStore(self.db) as m:
            self.assertEqual(m.remember_content(s, s.content + ("private value",)), 1)
        with MapStore(self.db) as m:
            result = m.summary("app.test", query="distinctive", limit=1)
            self.assertEqual(result["total_pages"], 1)
            self.assertEqual(result["pages"][0]["content_samples"][0]["label"], "Distinctive content target")
            self.assertEqual(result["pages"][0]["content_samples"][0]["source"], "observer")
            self.assertNotIn("private value", str(result))

    def test_reopened_route_prefers_content_page_over_incidental_favorites_cta(self):
        start = state(title="Home")
        product = State("app.test", "d1", "1", "product", (
            Node("pt", text="商品列表", bounds=(0, 0, 200, 40)),
            Node("list", role="container", scrollable=True, bounds=(0, 40, 200, 300)),
            Node("row19", parent_id="list", text="商品 19", bounds=(0, 50, 100, 90))), 200, 300)
        favorite = State("app.test", "d1", "1", "favorite", (
            Node("ft", text="收藏", bounds=(0, 0, 200, 40)),
            Node("cta", resource_id="app:open", text="打开商品 19", role="button", clickable=True,
                 bounds=(0, 50, 180, 100))), 200, 300)
        with MapStore(self.db) as m:
            start_id = m.observe(start)
            product_id = m.observe(product)
            favorite_id = m.observe(favorite)
            m.remember_content(product, product.content)
            m.connect(start, product, Action("CLICK", Selector(resource_id="tab_products")), True)
            m.connect(start, favorite, Action("CLICK", Selector(resource_id="tab_favorites")), True)
        with MapStore(self.db) as m:
            route = m.route("app.test", start_id, "商品 19", "d1")
            self.assertEqual(len(route), 1)
            self.assertEqual(route[0]["to"], product_id)
            self.assertNotEqual(route[0]["to"], favorite_id)
            self.assertEqual(route[0]["action"]["kind"], "CLICK")

    def test_same_title_navigation_tabs_do_not_stale_each_other(self):
        def tab_state(label, extra=False):
            base = list(state(title="商品", extra=extra).nodes)
            base.append(Node("nav", resource_id="app:tab", text=label, role="tab", selected=True,
                             clickable=True, bounds=(0, 250, 100, 300)))
            return State("app.test", "d1", "1", label, tuple(base), 200, 300)

        with MapStore(self.db) as m:
            recommended = m.observe(tab_state("推荐"))
            newest = m.observe(tab_state("新品"))
            self.assertNotEqual(recommended, newest)
            summaries = m.summary("app.test", device_id="d1")
            self.assertEqual({p["id"] for p in summaries["pages"]}, {recommended, newest})
            changed_recommended = m.observe(tab_state("推荐", extra=True))
            self.assertNotEqual(changed_recommended, recommended)
            rows = m.db.execute("SELECT page_id,stale,navigation_context FROM variants WHERE device='d1'").fetchall()
            states = {row["page_id"]: row["stale"] for row in rows}
            self.assertEqual(states[recommended], 1)
            self.assertEqual(states[newest], 0)
            self.assertEqual(states[changed_recommended], 0)

    def test_migrates_existing_variant_schema(self):
        with MapStore(self.db):
            pass
        import sqlite3
        db = sqlite3.connect(self.db)
        try:
            db.execute("DROP TABLE variants")
            db.execute("CREATE TABLE variants(page_id TEXT NOT NULL, device TEXT NOT NULL, version TEXT NOT NULL, title TEXT NOT NULL, controls TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL, observed REAL NOT NULL, stale INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(page_id,device,version))")
            db.commit()
        finally:
            db.close()
        with MapStore(self.db) as m:
            columns = {r["name"] for r in m.db.execute("PRAGMA table_info(variants)")}
            self.assertIn("navigation_context", columns)


if __name__ == "__main__": unittest.main()

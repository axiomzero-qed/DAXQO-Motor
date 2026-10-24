"""Small persistent map of observed app pages and verified navigation."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from .models import Action, State


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MapStore:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY, package TEXT NOT NULL,
            fingerprint TEXT NOT NULL, UNIQUE(package,fingerprint));
          CREATE TABLE IF NOT EXISTS variants(page_id TEXT NOT NULL, device TEXT NOT NULL,
            version TEXT NOT NULL, title TEXT NOT NULL, controls TEXT NOT NULL,
            source TEXT NOT NULL, confidence REAL NOT NULL, observed REAL NOT NULL,
            stale INTEGER NOT NULL DEFAULT 0, navigation_context TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(page_id,device,version));
          CREATE TABLE IF NOT EXISTS page_content(page_id TEXT NOT NULL, device TEXT NOT NULL,
            version TEXT NOT NULL, label TEXT NOT NULL, source TEXT NOT NULL,
            verified_at REAL NOT NULL, PRIMARY KEY(page_id,device,version,label));
          CREATE TABLE IF NOT EXISTS edges(id INTEGER PRIMARY KEY, package TEXT NOT NULL,
            src TEXT NOT NULL, dst TEXT NOT NULL, action TEXT NOT NULL, device TEXT NOT NULL,
            version TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
            verified_at REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
            invalidated INTEGER NOT NULL DEFAULT 0,
            UNIQUE(src,dst,action,device,version));
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
          INSERT OR IGNORE INTO meta VALUES('invalidated',0);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(variants)")}
        if "navigation_context" not in columns:
            self.db.execute("ALTER TABLE variants ADD COLUMN navigation_context TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def close(self):
        with self._lock: self.db.close()

    def bind_local_device(self, device_id):
        """Assign preview 'local' variants to this phone, preserving existing memory."""
        if not device_id or device_id == "local":
            return
        with self._lock, self.db:
            others = self.db.execute("SELECT DISTINCT device FROM variants WHERE device NOT IN ('local',?)", (device_id,)).fetchall()
            if others:
                return  # A shared multi-device database cannot disambiguate an old placeholder.
            for table, keys in (("variants", "page_id,version"), ("edges", "src,dst,action,version"),
                                ("page_content", "page_id,version,label")):
                # Already-bound rows are newer observations; leave historical conflicts untouched.
                conditions = " AND ".join(f"bound.{key}=pending.{key}" for key in keys.split(","))
                self.db.execute(f"UPDATE OR IGNORE {table} AS pending SET device=? WHERE device='local' AND NOT EXISTS "
                    f"(SELECT 1 FROM {table} AS bound WHERE bound.device=? AND {conditions})", (device_id, device_id))

    @staticmethod
    def _page_id(state):
        # page_fingerprint is already a stable content digest; include package explicitly.
        import hashlib
        return hashlib.sha256((state.package + ":" + state.page_fingerprint).encode()).hexdigest()[:24]

    @staticmethod
    def _safe_title(state):
        return next((n.label for n in state.visible if n.label and not n.editable
                     and not n.clickable and not n.scrollable and not state.under_scroll(n)), state.package)

    @staticmethod
    def _navigation_context(state):
        selected = []
        for node in state.visible:
            if (node.selected or node.checked) and not node.editable and not state.under_scroll(node):
                selector = node.selector().public()
                if selector:
                    selected.append(selector)
        return _json(sorted({ _json(item): item for item in selected }.values(), key=_json))

    def observe(self, state: State, source="scout", confidence=1.0) -> str:
        page_id = self._page_id(state)
        if state.excluded:
            return page_id
        controls = []
        for n in state.visible:
            if not (n.clickable or n.scrollable) or n.editable:
                continue
            # Never persist editable labels, even when a node is otherwise actionable.
            sel = n.selector().public()
            if n.editable:
                sel.pop("text", None); sel.pop("description", None)
            controls.append({"selector": sel, "bounds": list(n.bounds), "role": n.role,
                             "clickable": n.clickable, "scrollable": n.scrollable})
        now = time.time()
        with self._lock, self.db:
            prior = self.db.execute("SELECT v.version FROM variants v JOIN pages p ON p.id=v.page_id WHERE v.device=? AND p.package=? ORDER BY v.observed DESC LIMIT 1",
                                    (state.device_id, state.package)).fetchone()
            if prior and prior["version"] != state.version:
                self.db.execute("UPDATE variants SET stale=1 WHERE device=? AND page_id IN (SELECT id FROM pages WHERE package=?) AND version<>?",
                                (state.device_id, state.package, state.version))
                cur = self.db.execute("UPDATE edges SET invalidated=1 WHERE package=? AND device=? AND version<>? AND invalidated=0",
                                      (state.package, state.device_id, state.version))
                self.db.execute("UPDATE meta SET value=value+? WHERE key='invalidated'", (cur.rowcount,))
            # Structural page changes with the same title only stale this device's old variant.
            safe_title = self._safe_title(state)
            navigation_context = self._navigation_context(state)
            old = self.db.execute("SELECT p.id FROM pages p JOIN variants v ON v.page_id=p.id WHERE p.package=? AND v.device=? AND v.version=? AND v.title=? AND v.navigation_context=? AND p.id<>?",
                                  (state.package, state.device_id, state.version, safe_title, navigation_context, page_id)).fetchall()
            for row in old:
                self.db.execute("UPDATE variants SET stale=1 WHERE page_id=? AND device=? AND version=?",
                                 (row["id"], state.device_id, state.version))
                cur = self.db.execute("UPDATE edges SET invalidated=1 WHERE package=? AND device=? AND version=? AND (src=? OR dst=?) AND invalidated=0",
                                      (state.package, state.device_id, state.version, row["id"], row["id"]))
                self.db.execute("UPDATE meta SET value=value+? WHERE key='invalidated'", (cur.rowcount,))
            self.db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?)", (page_id,state.package,state.page_fingerprint))
            self.db.execute("INSERT INTO variants(page_id,device,version,title,controls,source,confidence,observed,stale,navigation_context) VALUES(?,?,?,?,?,?,?,?,0,?) ON CONFLICT(page_id,device,version) DO UPDATE SET title=excluded.title,controls=excluded.controls,source=excluded.source,confidence=excluded.confidence,observed=excluded.observed,stale=0,navigation_context=excluded.navigation_context",
                            (page_id,state.device_id,state.version,safe_title,_json(controls),source,float(confidence),now,navigation_context))
        return page_id

    def connect(self, before: State, after: State, action: Action, verified: bool, source="scout") -> None:
        src, dst = self._page_id(before), self._page_id(after)
        action_data = action.public()
        action_data.pop("id", None)
        action_data.pop("wait_ms", None)
        key = _json(action_data)
        now = time.time()
        with self._lock, self.db:
            if before.excluded or after.excluded: return
            self.db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?)", (src,before.package,before.page_fingerprint))
            self.db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?)", (dst,after.package,after.page_fingerprint))
            if verified:
                self.db.execute("INSERT INTO edges(package,src,dst,action,device,version,source,confidence,verified_at,failures,invalidated) VALUES(?,?,?,?,?,?,?,?,?,0,0) ON CONFLICT(src,dst,action,device,version) DO UPDATE SET source=excluded.source,confidence=excluded.confidence,verified_at=excluded.verified_at,invalidated=0",
                                (before.package,src,dst,key,before.device_id,before.version,source,1.0,now))
            else:
                edge_key = (src,key,before.device_id,before.version)
                fresh = self.db.execute("SELECT COUNT(*) FROM edges WHERE src=? AND action=? AND device=? AND version=? AND invalidated=0", edge_key).fetchone()[0]
                self.db.execute("UPDATE edges SET failures=failures+1,confidence=MAX(0,confidence*0.5),invalidated=1 WHERE src=? AND action=? AND device=? AND version=?", edge_key)
                self.db.execute("UPDATE meta SET value=value+? WHERE key='invalidated'", (fresh,))

    def remember_content(self, state: State, labels) -> int:
        """Remember at most 200 distinct, visible, non-editable scrolled labels per variant."""
        if state.excluded:
            return 0
        allowed = {n.label for n in state.visible if n.label and not n.editable and state.under_scroll(n)}
        values = list(dict.fromkeys(str(label).strip() for label in labels if label and str(label).strip() in allowed))[:200]
        if not values:
            return 0
        page_id = self._page_id(state)
        with self._lock:
            known = self.db.execute("SELECT 1 FROM variants WHERE page_id=? AND device=? AND version=?",
                                    (page_id,state.device_id,state.version)).fetchone()
        if not known:
            self.observe(state, source="content", confidence=0.5)
        now = time.time()
        title = self._safe_title(state)
        with self._lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?)", (page_id,state.package,state.page_fingerprint))
            self.db.execute("INSERT OR IGNORE INTO variants(page_id,device,version,title,controls,source,confidence,observed,stale) VALUES(?,?,?,?,?,?,?, ?,0)",
                            (page_id,state.device_id,state.version,title,"[]","content",0.5,now))
            count = self.db.execute("SELECT COUNT(*) FROM page_content WHERE page_id=? AND device=? AND version=?",
                                    (page_id,state.device_id,state.version)).fetchone()[0]
            added = 0
            for label in values:
                exists = self.db.execute("SELECT 1 FROM page_content WHERE page_id=? AND device=? AND version=? AND label=?",
                                         (page_id,state.device_id,state.version,label)).fetchone()
                if exists:
                    self.db.execute("UPDATE page_content SET verified_at=?,source='observer' WHERE page_id=? AND device=? AND version=? AND label=?",
                                    (now,page_id,state.device_id,state.version,label))
                elif count + added < 200:
                    self.db.execute("INSERT INTO page_content VALUES(?,?,?,?,?,?)",
                                    (page_id,state.device_id,state.version,label,"observer",now))
                    added += 1
            return added

    def summary(self, package, device_id=None, query=None, limit=12) -> dict:
        limit = max(1, int(limit))
        q = (query or "").casefold()
        where = "p.package=? AND v.stale=0"
        args = [package]
        if device_id:
            where += " AND v.device=?"; args.append(device_id)
        if q:
            where += " AND (LOWER(v.title) LIKE ? OR LOWER(v.controls) LIKE ? OR EXISTS (SELECT 1 FROM page_content c WHERE c.page_id=p.id AND c.device=v.device AND c.version=v.version AND LOWER(c.label) LIKE ?))"
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        with self._lock:
            total = self.db.execute(f"SELECT COUNT(DISTINCT p.id) FROM pages p JOIN variants v ON p.id=v.page_id WHERE {where}", args).fetchone()[0]
            selected = self.db.execute(f"SELECT p.id,MAX(v.observed) AS seen FROM pages p JOIN variants v ON p.id=v.page_id WHERE {where} GROUP BY p.id ORDER BY seen DESC LIMIT ?",
                                       (*args, limit)).fetchall()
            pageids = [r["id"] for r in selected]
            if pageids:
                rows = self.db.execute("SELECT p.id,v.device,v.version,v.title,v.controls,v.source,v.confidence,v.observed FROM pages p JOIN variants v ON p.id=v.page_id WHERE v.stale=0 AND p.id IN (" + ",".join("?" for _ in pageids) + ") ORDER BY v.observed DESC", pageids).fetchall()
            else:
                rows = []
            merged = {}
            for r in rows:
                page = merged.setdefault(r["id"], {"id": r["id"], "title": r["title"], "devices": [], "controls": []})
                page["devices"].append({"device_id": r["device"], "version": r["version"],
                                        "versions": [r["version"]], "source": r["source"],
                                        "confidence": r["confidence"], "last_observed": r["observed"]})
                seen = { _json(c["selector"]) for c in page["controls"] }
                for control in json.loads(r["controls"]):
                    selector = control.get("selector", {})
                    key = _json(selector)
                    if key in seen: continue
                    seen.add(key)
                    page["controls"].append({"selector": selector, "role": control.get("role", ""),
                                              "clickable": control.get("clickable", False),
                                              "scrollable": control.get("scrollable", False)})
                    if len(page["controls"]) >= 12: break
            pages = list(merged.values())[:limit]
            pageids = [p["id"] for p in pages]
            if pageids:
                content_args = list(pageids)
                content_sql = "SELECT c.page_id,c.label,c.source,c.verified_at FROM page_content c JOIN variants v ON v.page_id=c.page_id AND v.device=c.device AND v.version=c.version AND v.stale=0 WHERE c.page_id IN (" + ",".join("?" for _ in pageids) + ")"
                if device_id:
                    content_sql += " AND c.device=?"; content_args.append(device_id)
                content_sql += " ORDER BY verified_at DESC"
                content_rows = self.db.execute(content_sql, content_args).fetchall()
                by_page = {}
                for row in content_rows:
                    labels = by_page.setdefault(row["page_id"], {})
                    previous = labels.get(row["label"])
                    if previous is None or row["verified_at"] > previous["verified_at"]:
                        labels[row["label"]] = row
                for page in pages:
                    samples = list(by_page.get(page["id"], {}).values())
                    hits = [r for r in samples if q and q in r["label"].casefold()]
                    others = [r for r in samples if r not in hits]
                    page["content_samples"] = [{"label": r["label"], "source": r["source"], "last_verified": r["verified_at"]}
                                                for r in (hits + others)[:12]]
            else:
                for page in pages: page["content_samples"] = []
            edge_sql = "SELECT DISTINCT e.src,e.dst,e.action,e.device,e.version,e.confidence,e.verified_at,e.source FROM edges e JOIN variants sv ON sv.page_id=e.src AND sv.device=e.device AND sv.version=e.version AND sv.stale=0 JOIN variants dv ON dv.page_id=e.dst AND dv.device=e.device AND dv.version=e.version AND dv.stale=0 WHERE e.package=? AND e.invalidated=0"
            edge_args = [package]
            if device_id:
                edge_sql += " AND e.device=?"; edge_args.append(device_id)
            if pageids:
                edge_sql += " AND e.src IN (" + ",".join("?" for _ in pageids) + ") AND e.dst IN (" + ",".join("?" for _ in pageids) + ")"
                edge_args += pageids + pageids
            edge_sql += " ORDER BY e.verified_at DESC LIMIT ?"
            edges = self.db.execute(edge_sql, (*edge_args, limit * 2)).fetchall()
            public_edges = [{"from": r["src"], "to": r["dst"], "action": json.loads(r["action"]),
                             "device_id": r["device"], "version": r["version"],
                             "confidence": r["confidence"], "source": r["source"],
                             "last_verified": r["verified_at"]} for r in edges]
            return {"pages": pages, "total_pages": total,
                    "coverage": {"shown_pages": len(pages), "total_pages": total},
                    "edges": public_edges,
                    "entry_points": [p["id"] for p in pages if not any(e["to"] == p["id"] for e in public_edges)],
                    "invalidated": self.db.execute("SELECT value FROM meta WHERE key='invalidated'").fetchone()[0]}

    def route(self, package, start_page, target_query, device_id=None) -> list[dict]:
        q=target_query.casefold()
        with self._lock:
            pages=self.db.execute("SELECT p.id,v.title,v.controls,EXISTS(SELECT 1 FROM page_content c WHERE c.page_id=p.id AND c.device=v.device AND c.version=v.version AND LOWER(c.label) LIKE ?) AS content_match FROM pages p JOIN variants v ON p.id=v.page_id WHERE p.package=? AND v.stale=0 AND (? IS NULL OR v.device=?)",
                                  (f"%{q}%",package,device_id,device_id)).fetchall()
            title_targets = {r["id"] for r in pages if q in r["title"].casefold()}
            content_targets = {r["id"] for r in pages if r["content_match"]}
            control_targets = {r["id"] for r in pages if q in r["controls"].casefold()}
            # Actual stored list content is a stronger destination signal than a
            # generic button such as “Open item 19”; avoid routing into that CTA.
            targets = title_targets or content_targets or control_targets
            if start_page in targets: return []
            edges=self.db.execute("SELECT DISTINCT e.src,e.dst,e.action,e.device,e.version,e.confidence,e.source,e.verified_at FROM edges e JOIN variants sv ON sv.page_id=e.src AND sv.device=e.device AND sv.version=e.version AND sv.stale=0 JOIN variants dv ON dv.page_id=e.dst AND dv.device=e.device AND dv.version=e.version AND dv.stale=0 WHERE e.package=? AND e.invalidated=0 AND (? IS NULL OR e.device=?) AND json_extract(e.action,'$.kind') IN ('CLICK','BACK') ORDER BY e.verified_at DESC",
                                  (package,device_id,device_id)).fetchall()
        graph={}
        for e in edges: graph.setdefault(e["src"],[]).append(e)
        todo=deque([(start_page,[])]); seen={start_page}
        while todo:
            page,path=todo.popleft()
            for e in graph.get(page,[]):
                np=path+[ {"from":e["src"],"to":e["dst"],"action":json.loads(e["action"]),"device_id":e["device"],"version":e["version"],"confidence":e["confidence"],"source":e["source"],"last_verified":e["verified_at"]} ]
                if e["dst"] in targets: return np
                if e["dst"] not in seen: seen.add(e["dst"]); todo.append((e["dst"],np))
        return []

    def invalidate(self, package, page_id, device_id=None, reason="execution_failed") -> None:
        with self._lock, self.db:
            filt="package=? AND (src=? OR dst=?) AND invalidated=0"; args=[package,page_id,page_id]
            if device_id: filt += " AND device=?"; args.append(device_id)
            cur=self.db.execute(f"UPDATE edges SET invalidated=1,failures=failures+1,confidence=MAX(0,confidence*0.5) WHERE {filt}",args)
            self.db.execute("UPDATE meta SET value=value+? WHERE key='invalidated'",(cur.rowcount,))

    def stats(self) -> dict:
        with self._lock:
            return {"pages":self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0],"variants":self.db.execute("SELECT COUNT(*) FROM variants").fetchone()[0],"edges":self.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0],"verified_edges":self.db.execute("SELECT COUNT(*) FROM edges WHERE invalidated=0").fetchone()[0],"invalidated_edges":self.db.execute("SELECT COUNT(*) FROM edges WHERE invalidated=1").fetchone()[0],"failures":self.db.execute("SELECT COALESCE(SUM(failures),0) FROM edges").fetchone()[0]}

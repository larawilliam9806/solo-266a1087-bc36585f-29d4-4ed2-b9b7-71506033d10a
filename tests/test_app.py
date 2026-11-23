"""Flask-flow tests: archives, fallback, retirement and failure preservation."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="qc-test-")
os.environ["QC_DB"] = os.path.join(_TMP, "test.db")

import app as appmod  # noqa: E402


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        appmod.app.config.update(TESTING=True, DATABASE=os.environ["QC_DB"])
        appmod.init_db()

    def setUp(self):
        conn = self.db()
        conn.executescript(
            "DROP TRIGGER IF EXISTS fail_sum; DROP TRIGGER IF EXISTS fail_det;"
            "DELETE FROM determinations; DELETE FROM details; "
            "DELETE FROM summaries; DELETE FROM coils; DELETE FROM batches;"
        )
        conn.commit()
        conn.close()
        self.client = appmod.app.test_client()
        self.client.post("/batches", data={"batch_no": "B1"})
        self.bid = self.db().execute("SELECT id FROM batches").fetchone()["id"]

    def db(self):
        conn = sqlite3.connect(os.environ["QC_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def summary(self, coil, n, mn, p, mx):
        return self.client.post(
            f"/batch/{self.bid}/summary",
            data={"coil_no": coil, "point_count": n, "min_um": mn,
                  "p95_um": p, "max_um": mx},
            follow_redirects=True,
        )

    def detail(self, coil, raw):
        return self.client.post(
            f"/batch/{self.bid}/detail",
            content_type="multipart/form-data",
            data={"coil_no": coil, "readings": (io.BytesIO(raw), "c.csv")},
            follow_redirects=True,
        )

    def determine(self, limit):
        return self.client.post(
            f"/batch/{self.bid}/determine",
            data={"limit_um": limit}, follow_redirects=True,
        )

    def last_det(self):
        return self.db().execute(
            "SELECT * FROM determinations ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # ------------------------------------------------------------------

    def test_infeasible_summary_rejected(self):
        text = self.summary("C1", 2, 100, 200, 300).get_data(as_text=True)
        self.assertIn("无法", text)
        count = self.db().execute("SELECT COUNT(*) c FROM summaries").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_bounds_and_verdicts(self):
        self.summary("C1", 2, 100, 300, 300)
        self.summary("C2", 2, 200, 300, 300)
        self.determine(300)
        d = self.last_det()
        # Total 4 readings, k=4: P95 is the batch maximum, forced to 300.
        self.assertEqual((d["min_possible"], d["max_possible"]), (300, 300))
        self.assertEqual(d["verdict"], "must_not_exceed")

        self.determine(250)
        self.assertEqual(self.last_det()["verdict"], "must_exceed")

    def test_needs_detail_band(self):
        # Total 40 readings, k=38.  C1 (20 points) never exceeds 300, so the
        # low end is 300; C2's P95=500 pushes the high end to 500.
        self.summary("C1", 20, 100, 300, 300)
        self.summary("C2", 20, 100, 500, 500)
        self.determine(400)
        d = self.last_det()
        self.assertEqual((d["min_possible"], d["max_possible"]), (300, 500), dict(d))
        self.assertEqual(d["verdict"], "needs_detail", dict(d))
        self.determine(299)
        self.assertEqual(self.last_det()["verdict"], "must_exceed")
        self.determine(500)
        self.assertEqual(self.last_det()["verdict"], "must_not_exceed")

    def test_matching_detail_is_used(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"reading,min,p95,max\n100,100,300,300\n300,100,300,300\n")
        row = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(row["active"], 1)
        self.determine(300)
        snap = json.loads(self.last_det()["snapshot_json"])
        self.assertEqual({c["coil_no"]: c["kind"] for c in snap["coils"]},
                         {"C1": "detail"})

    def test_mismatched_detail_is_archived_inactive(self):
        self.summary("C1", 2, 100, 300, 300)
        text = self.detail("C1", b"100\n200\n300\n").get_data(as_text=True)
        self.assertIn("已停用", text)
        row = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(row["active"], 0)
        self.assertIn("与摘要不符", row["retired_reason"])
        self.determine(300)
        snap = json.loads(self.last_det()["snapshot_json"])
        self.assertEqual(snap["coils"][0]["kind"], "summary")

    def test_summary_change_retires_unmatched_detail(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        text = self.summary("C1", 3, 100, 300, 300).get_data(as_text=True)
        self.assertIn("已停用", text)
        self.assertIn("改按新摘要核算", text)
        detail = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(detail["active"], 0)
        active = self.db().execute(
            "SELECT * FROM summaries WHERE active = 1"
        ).fetchall()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["point_count"], 3)

    def test_summary_change_keeps_matching_detail(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        text = self.summary("C1", 2, 100, 300, 300).get_data(as_text=True)
        self.assertIn("明细继续有效", text)
        detail = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(detail["active"], 1)

    def test_detail_without_summary_refused(self):
        text = self.detail("C1", b"100\n300\n").get_data(as_text=True)
        self.assertIn("请先登记摘要", text)
        count = self.db().execute("SELECT COUNT(*) c FROM details").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_history_snapshot_is_immutable(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        first = self.last_det()
        self.summary("C1", 2, 100, 500, 500)   # change the archive afterwards
        again = self.db().execute(
            "SELECT * FROM determinations WHERE id = ?", (first["id"],)
        ).fetchone()
        self.assertEqual(again["snapshot_json"], first["snapshot_json"])
        self.assertEqual(again["min_possible"], 300)

    def test_invalid_limit_writes_nothing(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        before = self.db().execute("SELECT COUNT(*) c FROM determinations").fetchone()["c"]
        self.determine("abc")
        after = self.db().execute("SELECT COUNT(*) c FROM determinations").fetchone()["c"]
        self.assertEqual(before, after)

    def test_write_failure_rolls_back(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        conn = self.db()
        conn.execute(
            "CREATE TRIGGER fail_sum BEFORE INSERT ON summaries "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
        conn.close()
        self.summary("C1", 3, 100, 300, 300)
        conn = self.db()
        rows = conn.execute(
            "SELECT point_count, active FROM summaries ORDER BY id"
        ).fetchall()
        detail = conn.execute("SELECT active FROM details").fetchone()
        conn.close()
        self.assertEqual([tuple(r) for r in rows], [(2, 1)])
        self.assertEqual(detail["active"], 1)

    def test_determination_failure_keeps_previous(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        conn = self.db()
        conn.execute(
            "CREATE TRIGGER fail_det BEFORE INSERT ON determinations "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
        conn.close()
        text = self.determine(999).get_data(as_text=True)
        self.assertIn("原档案保持不变", text)
        count = self.db().execute("SELECT COUNT(*) c FROM determinations").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_detail_is_read_once_per_determination(self):
        import qc as qcmod

        self.summary("C1", 3, 100, 300, 300)
        self.detail("C1", b"100\n300\n300\n")
        self.summary("C2", 2, 100, 300, 300)
        self.detail("C2", b"100\n300\n")

        loads = []
        real_loads = appmod.DetailCounterFactory.load

        def counting_load(factory, detail_id, raw):
            loads.append(detail_id)
            return real_loads(factory, detail_id, raw)

        appmod.DetailCounterFactory.load = counting_load
        try:
            self.determine(300)
        finally:
            appmod.DetailCounterFactory.load = real_loads
        self.assertEqual(len(loads), 2)
        self.assertEqual(len(set(loads)), 2)

    def test_corrupt_detail_falls_back_to_summary(self):
        self.summary("C1", 3, 100, 300, 300)
        self.detail("C1", b"100\n300\n300\n")
        conn = self.db()
        conn.execute("UPDATE details SET readings = '[100, 300]'")
        conn.commit()
        conn.close()
        self.determine(300)
        snap = json.loads(self.last_det()["snapshot_json"])
        self.assertEqual(snap["coils"][0]["kind"], "summary")
        self.assertTrue(any("退回摘要" in n for n in snap["notes"]), snap["notes"])

    def test_batch_page_does_not_load_detail_readings(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        conn = self.db()
        conn.execute("UPDATE details SET readings = 'not-json'")
        conn.commit()
        conn.close()
        text = self.client.get(f"/batch/{self.bid}").get_data(as_text=True)
        self.assertIn("C1", text)
        self.assertNotIn("not-json", text)

    def test_disk_counter_matches_sorted_readings(self):
        import qc as qcmod

        vals = [300, 100, 150, 150, 280, 150, 150, 150, 150, 150]
        n, mn, p, mx = qcmod.readings_stats(vals)
        factory = appmod.DetailCounterFactory()
        try:
            counter = factory.load(1, json.dumps(vals))
            self.assertIsNotNone(counter)
            self.assertEqual((counter.n, counter.mn, counter.p, counter.mx),
                             (n, mn, p, mx))
            ordered = sorted(vals)
            for t in range(90, 320):
                le = sum(v <= t for v in ordered)
                ge = sum(v >= t for v in ordered)
                self.assertEqual(counter.count_le(t), le, ("le", t))
                self.assertEqual(counter.count_ge(t), ge, ("ge", t))
        finally:
            factory.close()

    def test_disk_counter_handles_multiple_details_in_one_file(self):
        factory = appmod.DetailCounterFactory()
        try:
            first = factory.load(1, json.dumps([5, 1, 9, 3]))
            second = factory.load(2, json.dumps(list(range(100, 300))))
            self.assertEqual(first.count_le(4), 2)
            self.assertEqual(first.count_ge(5), 2)
            self.assertEqual(second.count_le(199), 100)
            self.assertEqual(second.count_ge(199), 101)
            # The first counter is unaffected by appending the second.
            self.assertEqual(first.count_le(9), 4)
        finally:
            factory.close()

    def test_determination_uses_exact_detail_counts(self):
        # C1 has a real detail whose duplicate-heavy shape would be hidden by
        # a counter that answered every query with 0 or n.
        vals = [100] + [150] * 17 + [280, 300]
        self.summary("C1", 20, 100, 280, 300)
        raw = ("\n".join(str(v) for v in vals) + "\n").encode()
        self.detail("C1", raw)
        self.summary("C2", 20, 100, 500, 500)
        self.determine(400)
        d = self.last_det()
        # 40 readings, k=38.  C1 can put at most 19 readings at or below 280,
        # C2's P95=500 keeps its 20th point at 500, so 38 readings cannot all
        # be <= 280: the low end is forced to 300.
        self.assertEqual((d["min_possible"], d["max_possible"]), (300, 500))
        self.assertEqual(d["verdict"], "needs_detail")
        snap = json.loads(d["snapshot_json"])
        self.assertEqual(snap["coils"][0]["kind"], "detail")

    def test_out_of_memory_during_determine_saves_nothing(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        self.determine(300)
        count = self.db().execute(
            "SELECT COUNT(*) c FROM determinations"
        ).fetchone()["c"]

        real_load = appmod.DetailCounterFactory.load

        def out_of_memory(factory, detail_id, raw):
            raise MemoryError()

        appmod.DetailCounterFactory.load = out_of_memory
        try:
            text = self.determine(999).get_data(as_text=True)
        finally:
            appmod.DetailCounterFactory.load = real_load
        self.assertIn("未保存判定", text)
        self.assertEqual(
            self.db().execute("SELECT COUNT(*) c FROM determinations").fetchone()["c"],
            count,
        )
        # The earlier determination is still readable.
        old = self.db().execute(
            "SELECT id FROM determinations ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertEqual(
            self.client.get(
                f"/batch/{self.bid}/determination/{old['id']}"
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()

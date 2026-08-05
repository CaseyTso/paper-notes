"""Read-only Zotero migration adapter tests (Task 8).

Frozen after the first red run; do not weaken or delete assertions.
The adapter must never serialize Zotero source keys into canonical notes:
records stay in-memory migration objects only.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_notes.adapters import zotero


def build_fixture(root: Path) -> Path:
    """Minimum Zotero schema faithful to the real layout: imported
    attachments live in ``storage/<attachmentItemKey>/<filename>``,
    linked URLs live in the attachment item's ``url`` field, linkMode is
    an INTEGER (0-3; unknown modes possible), creator fieldMode is 0/1
    (1 = literal/group author), and annotation is a real item type."""
    db = root / "zotero.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY,
            parentItemID INTEGER, linkMode INTEGER, path TEXT, contentType TEXT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER);
        CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT);
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER);

        INSERT INTO itemTypes (itemTypeID, typeName) VALUES
            (1, 'journalArticle'), (2, 'attachment'), (3, 'note'), (4, 'annotation');
        INSERT INTO items (itemID, key, itemTypeID) VALUES
            (1, 'PARENT1', 1), (2, 'ATTACH1', 2), (3, 'ATTACH2', 2),
            (4, 'ATTACH3', 2), (5, 'ATTACH4', 2), (6, 'NOTE1', 3),
            (7, 'PARENT2', 1), (8, 'ATTACH5', 2), (9, 'PARENT3', 1),
            (10, 'ANN1', 4), (11, 'ATTACH6', 2), (12, 'ATTACH7', 2),
            (13, 'EMBED', 2), (14, '../../EVIL', 2);
        INSERT INTO fields (fieldID, fieldName) VALUES
            (1, 'title'), (2, 'date'), (110, 'citationKey'), (35, 'url'),
            (36, 'DOI'), (37, 'publicationTitle');
        INSERT INTO itemDataValues (valueID, value) VALUES
            (1001, 'Congenital multiple eventrations of the right diaphragm'),
            (1002, '2014-05-01'),
            (1003, 'wuCongenitalMultipleEventrations2014'),
            (1004, 'Parent Two Title'),
            (1005, '2020'),
            (1006, 'A Note Body'),
            (1007, 'Parent Three Title'),
            (1008, 'https://doi.org/10.1000/xyz'),
            (1009, '10.1000/congenital-diaphragm'),
            (1010, 'Journal of Thoracic Disease');
        INSERT INTO itemData (itemID, fieldID, valueID) VALUES
            (1, 1, 1001), (1, 2, 1002), (1, 110, 1003),
            (1, 36, 1009), (1, 37, 1010),
            (7, 1, 1004), (7, 2, 1005),
            (6, 1, 1006),
            (9, 1, 1007),
            (4, 35, 1008);
        INSERT INTO itemAttachments (itemID, parentItemID, linkMode, path, contentType) VALUES
            (2, 1, 0, 'storage:ATTACH1.pdf', 'application/pdf'),
            (3, 1, 2, '/abs/path/ATTACH2.pdf', 'application/pdf'),
            (4, 1, 3, NULL, 'application/pdf'),
            (5, 1, 0, 'storage:MISSING.pdf', 'application/pdf'),
            (8, 7, 0, 'storage:ATTACH5.pdf', 'application/pdf'),
            (11, 1, 0, 'storage:../../outside.pdf', 'application/pdf'),
            (12, 1, 4, 'storage:ATTACH7.pdf', 'text/html'),
            (13, 6, 0, 'storage:EMBED.png', 'image/png'),
            (14, 1, 0, 'storage:EVIL.pdf', 'application/pdf');
        INSERT INTO creators (creatorID, firstName, lastName, fieldMode) VALUES
            (1, 'Xiao', 'Wu', 0), (2, '', 'American Heart Association', 1),
            (3, '', 'World Health Organization', 1);
        INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (1, 'author');
        INSERT INTO itemCreators (itemID, creatorID, creatorTypeID, orderIndex) VALUES
            (1, 1, 1, 0), (9, 3, 1, 0);
        INSERT INTO tags (tagID, name) VALUES (1, 'to-read');
        INSERT INTO itemTags (itemID, tagID) VALUES (1, 1);
        INSERT INTO collections (collectionID, collectionName) VALUES (1, 'My Library');
        INSERT INTO collectionItems (collectionID, itemID) VALUES (1, 1);
        """
    )
    conn.commit()
    conn.close()

    # real layout: storage/<attachmentItemKey>/<filename>
    storage = root / "storage"
    (storage / "ATTACH1").mkdir(parents=True)
    (storage / "ATTACH1" / "ATTACH1.pdf").write_bytes(b"%PDF-1.4 fake attachment")
    (storage / "ATTACH5").mkdir()
    (storage / "ATTACH5" / "ATTACH5.pdf").write_bytes(b"%PDF-1.4 fake attachment")
    (root / "linked_ATTACH2.pdf").write_bytes(b"%PDF-1.4 linked file")
    return db


class ZoteroAdapterTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.db = build_fixture(self.root)

    def tearDown(self):
        self.td.cleanup()

    def _adapter(self):
        return zotero.ZoteroAdapter(db_path=self.db, data_dir=self.root)

    def test_db_copied_before_read_only_open(self):
        original = self.db.read_bytes()
        ad = self._adapter()
        try:
            self.assertTrue(ad.tmp_db.exists())
            self.assertNotEqual(ad.tmp_db.resolve(), self.db.resolve())
            self.assertEqual(self.db.read_bytes(), original)
        finally:
            ad.close()

    def test_parent_bibliographic_item_extraction(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("ATTACH1")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.source_key, "PARENT1")
            self.assertEqual(rec.title, "Congenital multiple eventrations of the right diaphragm")
            self.assertEqual(rec.citation_key, "wuCongenitalMultipleEventrations2014")
            self.assertEqual(rec.year, "2014")
            self.assertEqual(rec.creators, [{"family": "Wu", "given": "Xiao"}])
        finally:
            ad.close()

    def test_bibliographic_key_extraction_direct(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            self.assertEqual(rec.citation_key, "wuCongenitalMultipleEventrations2014")
            rec2 = ad.resolve("PARENT2")
            self.assertIsNone(rec2.citation_key)
        finally:
            ad.close()

    def test_record_exposes_doi_and_journal(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            self.assertEqual(rec.doi, "10.1000/congenital-diaphragm")
            self.assertEqual(rec.journal, "Journal of Thoracic Disease")
        finally:
            ad.close()

    def test_resolve_by_citation_key(self):
        ad = self._adapter()
        try:
            records = ad.resolve_by_citation_keys(
                ["wuCongenitalMultipleEventrations2014"]
            )
            rec = records["wuCongenitalMultipleEventrations2014"]
            self.assertIsNotNone(rec)
            self.assertEqual(rec.source_key, "PARENT1")
            self.assertEqual(rec.title, "Congenital multiple eventrations of the right diaphragm")
            self.assertEqual(rec.year, "2014")
            self.assertEqual(rec.creators, [{"family": "Wu", "given": "Xiao"}])
            self.assertEqual(rec.doi, "10.1000/congenital-diaphragm")
            pdfs = [a for a in rec.attachments if a.content_type == "application/pdf" and a.exists]
            self.assertGreaterEqual(len(pdfs), 1)
        finally:
            ad.close()

    def test_resolve_by_citation_keys_batch_with_missing(self):
        ad = self._adapter()
        try:
            records = ad.resolve_by_citation_keys(
                [
                    "wuCongenitalMultipleEventrations2014",
                    "GHOSTCITEKEY2026",
                ]
            )
            self.assertIn("wuCongenitalMultipleEventrations2014", records)
            self.assertIn("GHOSTCITEKEY2026", records)
            self.assertIsNone(records["GHOSTCITEKEY2026"])
        finally:
            ad.close()

    def test_resolve_by_citation_keys_empty(self):
        ad = self._adapter()
        try:
            self.assertEqual(ad.resolve_by_citation_keys([]), {})
        finally:
            ad.close()

    def test_imported_attachment_resolved_into_item_key_subdirectory(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            a1 = [a for a in rec.attachments if a.item_key == "ATTACH1"][0]
            self.assertEqual(a1.link_mode, "imported_file")
            self.assertEqual(
                a1.path,
                str((self.root / "storage" / "ATTACH1" / "ATTACH1.pdf").resolve()),
            )
            self.assertTrue(a1.exists)
            self.assertEqual(a1.content_type, "application/pdf")
        finally:
            ad.close()

    def test_linked_url_read_from_url_field(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            a3 = [a for a in rec.attachments if a.item_key == "ATTACH3"][0]
            self.assertEqual(a3.link_mode, "linked_url")
            self.assertEqual(a3.path, "https://doi.org/10.1000/xyz")
            self.assertFalse(a3.exists)
        finally:
            ad.close()

    def test_linked_file_absolute_path_kept(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            a2 = [a for a in rec.attachments if a.item_key == "ATTACH2"][0]
            self.assertEqual(a2.link_mode, "linked_file")
            self.assertTrue(a2.path.startswith("/"))
        finally:
            ad.close()

    def test_missing_attachment_file(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            missing = [a for a in rec.attachments if a.item_key == "ATTACH4"][0]
            self.assertEqual(missing.link_mode, "imported_file")
            self.assertFalse(missing.exists)
        finally:
            ad.close()

    def test_imported_path_traversal_rejected(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            evil = [a for a in rec.attachments if a.item_key == "ATTACH6"][0]
            self.assertIsNone(evil.path)  # never resolved outside storage
            self.assertFalse(evil.exists)
        finally:
            ad.close()

    def test_unknown_link_mode_preserved_explicitly(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            unk = [a for a in rec.attachments if a.item_key == "ATTACH7"][0]
            self.assertEqual(unk.link_mode, "unknown(4)")
            self.assertIsNone(unk.path)
            self.assertFalse(unk.exists)
            self.assertEqual(unk.content_type, "text/html")
        finally:
            ad.close()

    def test_no_note_tag_collection_annotation_extraction(self):
        ad = self._adapter()
        try:
            self.assertIsNone(ad.resolve("NOTE1"))
            self.assertIsNone(ad.resolve("ANN1"))
            rec = ad.resolve("PARENT1")
            self.assertNotIn("tags", rec.__dict__)
            self.assertNotIn("collections", rec.__dict__)
            self.assertNotIn("annotations", rec.__dict__)
            conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            n_tags = conn.execute("SELECT COUNT(*) FROM itemTags").fetchone()[0]
            n_cols = conn.execute("SELECT COUNT(*) FROM collectionItems").fetchone()[0]
            conn.close()
            self.assertGreater(n_tags, 0)
            self.assertGreater(n_cols, 0)
        finally:
            ad.close()

    def test_group_author_literal_via_field_mode_1(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT3")
            self.assertEqual(rec.creators, [{"literal": "World Health Organization"}])
        finally:
            ad.close()

    def test_wal_present_rejected_structurally(self):
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"\x00" * 1024)
        with self.assertRaises(zotero.ZoteroSnapshotError):
            self._adapter()

    def test_empty_wal_allowed(self):
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"")
        ad = self._adapter()
        try:
            self.assertIsNotNone(ad.resolve("PARENT1"))
        finally:
            ad.close()

    def test_constructor_failure_cleans_temp_copy(self):
        tmp_files_before = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite"))
        with mock.patch("paper_notes.adapters.zotero.shutil.copy2", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                self._adapter()
        leftovers = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite")) - tmp_files_before
        self.assertEqual(leftovers, set())

    def test_connect_failure_cleans_temp_copy(self):
        tmp_files_before = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite"))
        with mock.patch("paper_notes.adapters.zotero.sqlite3.connect", side_effect=sqlite3.Error("connect failed")):
            with self.assertRaises(sqlite3.Error):
                self._adapter()
        leftovers = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite")) - tmp_files_before
        self.assertEqual(leftovers, set())

    def test_close_unlink_retries_then_raises_without_closing_state(self):
        ad = self._adapter()
        tmp = ad.tmp_db
        real_unlink = Path.unlink
        calls = [0]

        def flaky(self, *args, **kwargs):
            if calls[0] == 0:
                calls[0] = 1
                raise PermissionError("busy")
            return real_unlink(self, *args, **kwargs)

        # a single failed unlink raises strictly and leaves the adapter
        # open for retry (no silent close)
        with mock.patch.object(Path, "unlink", flaky):
            with self.assertRaises(zotero.ZoteroCleanupError):
                ad.close()
        self.assertFalse(ad._closed)
        self.assertTrue(tmp.exists())
        ad.close()  # real unlink: caller retry succeeds
        self.assertFalse(tmp.exists())

        ad2 = self._adapter()
        tmp2 = ad2.tmp_db
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("busy")):
            with self.assertRaises(zotero.ZoteroCleanupError):
                ad2.close()
        # not silently marked closed: caller can retry
        self.assertTrue(tmp2.exists())
        self.assertFalse(ad2._closed)
        ad2.close()
        self.assertFalse(tmp2.exists())

    def test_attachment_parent_must_be_bibliographic(self):
        # real library: 128 linkMode=4 attachments are parents of notes;
        # an embedded-image key must never pull note content into records
        ad = self._adapter()
        try:
            self.assertIsNone(ad.resolve("EMBED"))
        finally:
            ad.close()

    def test_attachment_key_traversal_rejected(self):
        # the attachment item key itself must be a single path segment
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            evil = [a for a in rec.attachments if a.item_key == "../../EVIL"][0]
            self.assertIsNone(evil.path)
            self.assertFalse(evil.exists)
        finally:
            ad.close()

    def test_constructor_cleanup_failure_reported(self):
        tmp_files_before = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite"))
        with mock.patch("paper_notes.adapters.zotero.shutil.copy2", side_effect=OSError("copy failed")):
            with mock.patch.object(Path, "unlink", side_effect=PermissionError("unlink failed")):
                with self.assertRaises(Exception) as cm:
                    self._adapter()
        # original exception chain preserved, cleanup failure explicit
        self.assertIsInstance(cm.exception.__cause__, OSError)
        # deliberately-created residue is removed with the real unlink once
        # the mock is gone, and the shared temp dir ends with zero additions
        for leftover in set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite")) - tmp_files_before:
            leftover.unlink(missing_ok=True)
        self.assertEqual(
            set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite")) - tmp_files_before,
            set(),
        )

    def test_constructor_connect_failure_cleans_sidecars(self):
        # sqlite3.connect() creating -wal/-shm then raising must still
        # remove main + both sidecars
        tmp_files_before = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*"))

        def connect_then_fail(*args, **kwargs):
            tmp = Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite")
            target = max(tmp, key=lambda p: p.stat().st_mtime)
            Path(str(target) + "-wal").write_bytes(b"x")
            Path(str(target) + "-shm").write_bytes(b"y")
            raise sqlite3.OperationalError("connect boom")

        with mock.patch("paper_notes.adapters.zotero.sqlite3.connect", side_effect=connect_then_fail):
            with self.assertRaises(sqlite3.OperationalError):
                self._adapter()
        leftovers = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*")) - tmp_files_before
        self.assertEqual(leftovers, set())

    def test_sidecar_unlink_failure_raises_with_temp_path(self):
        ad = self._adapter()
        tmp = ad.tmp_db
        wal = Path(str(tmp) + "-wal")
        shm = Path(str(tmp) + "-shm")
        wal.write_bytes(b"x")
        shm.write_bytes(b"y")
        real_unlink = Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            if str(self).endswith("-wal"):
                raise PermissionError("wal busy")
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky_unlink):
            with self.assertRaises(zotero.ZoteroCleanupError) as cm:
                ad.close()
        self.assertIn(str(tmp), str(cm.exception))  # temp_path in the error
        self.assertTrue(wal.exists())  # not silently marked closed
        self.assertFalse(ad._closed)
        # retry succeeds with the real unlink
        ad.close()
        self.assertFalse(tmp.exists())
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

    def test_close_connection_error_still_unlinks(self):
        ad = self._adapter()
        tmp = ad.tmp_db
        real_conn = ad._conn  # swapped out below; close it explicitly

        class BoomConn:
            def close(self):
                raise RuntimeError("close boom")

        ad._conn = BoomConn()
        self.assertIsNotNone(real_conn)
        try:
            with self.assertRaises(RuntimeError):
                ad.close()
        finally:
            real_conn.close()  # avoid leaking the real sqlite connection
        self.assertFalse(tmp.exists())

    def test_close_cleans_wal_shm_sidecars(self):
        ad = self._adapter()
        wal = Path(str(ad.tmp_db) + "-wal")
        shm = Path(str(ad.tmp_db) + "-shm")
        wal.write_bytes(b"x")
        shm.write_bytes(b"y")
        ad.close()
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        self.assertFalse(ad.tmp_db.exists())

    def test_wal_toctou_during_copy_rejected(self):
        # WAL created between the pre-copy check and the copy completion
        tmp_files_before = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite"))
        real_copy2 = zotero.shutil.copy2

        def racing_copy2(src, dst):
            real_copy2(src, dst)
            Path(str(src) + "-wal").write_bytes(b"\x00" * 64)

        with mock.patch("paper_notes.adapters.zotero.shutil.copy2", side_effect=racing_copy2):
            with self.assertRaises(zotero.ZoteroSnapshotError):
                self._adapter()
        # the adapter cleans up ITS temp copy and never touches the source
        leftovers = set(Path(tempfile.gettempdir()).glob("paper-notes-zotero-*.sqlite")) - tmp_files_before
        self.assertEqual(leftovers, set())
        Path(str(self.db) + "-wal").unlink()

    def test_source_changed_during_copy_rejected(self):
        real_copy2 = zotero.shutil.copy2

        def mutating_copy2(src, dst):
            real_copy2(src, dst)
            with open(src, "ab") as fh:
                fh.write(b"tampered")

        with mock.patch("paper_notes.adapters.zotero.shutil.copy2", side_effect=mutating_copy2):
            with self.assertRaises(zotero.ZoteroSnapshotError):
                self._adapter()
        # restore the source so later tests are unaffected
        self.db.write_bytes(self.db.read_bytes()[:-len(b"tampered")])

    def test_temporary_copy_deleted_on_success_and_error(self):
        ad = self._adapter()
        tmp = ad.tmp_db
        self.assertTrue(tmp.exists())
        ad.close()
        self.assertFalse(tmp.exists())
        ad.close()  # idempotent

        ad2 = self._adapter()
        tmp2 = ad2.tmp_db
        with self.assertRaises(RuntimeError):
            with ad2:
                raise RuntimeError("boom")
        self.assertFalse(tmp2.exists())

    def test_unknown_item_key_returns_none(self):
        ad = self._adapter()
        try:
            self.assertIsNone(ad.resolve("GHOSTKEY"))
        finally:
            ad.close()

    def test_record_has_no_note_serialization_surface(self):
        ad = self._adapter()
        try:
            rec = ad.resolve("PARENT1")
            self.assertFalse(hasattr(rec, "to_markdown"))
            self.assertFalse(hasattr(rec, "to_frontmatter"))
            self.assertFalse(hasattr(rec, "to_yaml"))
        finally:
            ad.close()


if __name__ == "__main__":
    unittest.main()

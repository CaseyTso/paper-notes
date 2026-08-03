"""Local PDF identifier extraction and streaming hash tests (Task 10).

Frozen after the first red run; do not weaken or delete assertions.
Synthetic PDFs are built with PyMuPDF in isolated temporary
directories — no network, no real PDFs, no OCR, no AI. Every
immutability assertion pins the contract that the input file's bytes,
mode, mtime, and inode never change on success or failure paths.
"""

import hashlib
import os
import stat as stat_module
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import fitz

import paper_notes.pdf as pdf_module
from paper_notes.identifiers import ParsedIdentifier
from paper_notes.pdf import (
    HASH_CHUNK_SIZE,
    SCAN_PAGE_RANGE,
    PdfError,
    PdfIdentifierResult,
    extract_pdf_identifiers,
    sha256_stream,
)

DOI = "10.1000/xyz123"
PMID = "28845751"

_XMP_TEMPLATE = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "{identifier_block}"
    "</rdf:Description></rdf:RDF></x:xmpmeta>"
)

XMP_DOI_IN_BAG = _XMP_TEMPLATE.format(
    identifier_block=f"<dc:identifier><rdf:Bag><rdf:li>doi:{DOI}</rdf:li></rdf:Bag></dc:identifier>"
)
XMP_DOI_DIRECT = _XMP_TEMPLATE.format(
    identifier_block=f"<dc:identifier>{DOI}</dc:identifier>"
)
# dc:creator/rdf:Seq carrying a bare 6-9 digit run must never be scanned.
XMP_CREATOR_SEQ = _XMP_TEMPLATE.format(
    identifier_block=(
        "<dc:identifier><rdf:Bag><rdf:li>doi:10.1000/xyz123</rdf:li></rdf:Bag></dc:identifier>"
        "<dc:creator><rdf:Seq><rdf:li>28845751</rdf:li></rdf:Seq></dc:creator>"
    )
)
# Any legal namespace prefix on the identifier element is accepted.
XMP_ARBITRARY_PREFIX = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description xmlns:myns="http://example.org/myns/">'
    "<myns:identifier>doi:10.1000/xyz123</myns:identifier>"
    "</rdf:Description></rdf:RDF></x:xmpmeta>"
)
XMP_PMID_LABEL_IN_BAG = _XMP_TEMPLATE.format(
    identifier_block=f"<dc:identifier><rdf:Bag><rdf:li>PMID: {PMID}</rdf:li></rdf:Bag></dc:identifier>"
)
# The journal-pipeline carrier <prism:doi> (element local name ``doi``).
XMP_DOI_PRISM = _XMP_TEMPLATE.format(
    identifier_block=(
        '<prism:doi xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">'
        f"{DOI}</prism:doi>"
    )
)
# <dc:identifier rdf:resource="https://doi.org/..."/> — the RDF
# resource attribute on the identifier element itself.
XMP_DOI_RESOURCE_ATTR = _XMP_TEMPLATE.format(
    identifier_block=(
        '<dc:identifier rdf:resource="https://doi.org/10.1000/xyz123"/>'
    )
)
# Local-name matching must be case-insensitive (``dc:IDENTIFIER``).
XMP_UPPERCASE_IDENTIFIER = _XMP_TEMPLATE.format(
    identifier_block=(
        "<dc:IDENTIFIER><rdf:Bag><rdf:li>doi:10.1000/xyz123</rdf:li></rdf:Bag></dc:IDENTIFIER>"
    )
)


def make_pdf(path, *, pages=1, texts=(), xmp=None):
    """Build a synthetic PDF with ``texts[i]`` on page ``i`` and optional XMP."""
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=612, height=792)
        if index < len(texts) and texts[index]:
            page.insert_text((72, 72), texts[index])
    if xmp is not None:
        doc.set_xml_metadata(xmp)
    doc.save(str(path))
    doc.close()
    return path


def _svg_with_doi_bytes():
    """A minimal SVG whose text layer carries a DOI (rendered by PyMuPDF)."""
    return (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        b'<text x="10" y="40">DOI 10.1000/xyz123</text></svg>'
    )


def _png_bytes():
    """A minimal valid 1x1 PNG (opens fine in PyMuPDF, no text layer)."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def make_encrypted_pdf(path, *, text="DOI 10.1000/xyz123"):
    """Build an AES-256 password-protected PDF (``needs_pass == 1``)."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), text)
    doc.save(
        str(path),
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
        permissions=0,
    )
    doc.close()
    return path


def snapshot(path):
    st = path.stat()
    return {
        "bytes": path.read_bytes(),
        "mode": stat_module.S_IMODE(st.st_mode),
        "mtime_ns": st.st_mtime_ns,
        "ino": st.st_ino,
    }


def doi(value, original):
    return ParsedIdentifier(kind="doi", value=value, original=original)


def pmid(value, original):
    return ParsedIdentifier(kind="pmid", value=value, original=original)


class Sha256StreamTest(unittest.TestCase):
    def test_matches_stdlib_hash_on_small_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.pdf"
            payload = b"%PDF-1.7\n" + os.urandom(4096)
            path.write_bytes(payload)
            self.assertEqual(sha256_stream(path), hashlib.sha256(payload).hexdigest())

    def test_matches_stdlib_hash_across_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.pdf"
            payload = os.urandom(2 * HASH_CHUNK_SIZE + 777)
            path.write_bytes(payload)
            self.assertEqual(sha256_stream(path), hashlib.sha256(payload).hexdigest())

    def test_hash_chunk_size_constant_is_fixed(self):
        self.assertEqual(HASH_CHUNK_SIZE, 1 << 20)


class XmpExtractionTest(unittest.TestCase):
    def test_doi_in_xmp_bag_is_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(
                Path(tmp) / "xmp_bag.pdf", texts=("Nothing in the text.",), xmp=XMP_DOI_IN_BAG
            )
            result = extract_pdf_identifiers(path)
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIsInstance(result, PdfIdentifierResult)
        self.assertEqual(result.identifiers, (doi(DOI, f"doi:{DOI}"),))
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.sources, ("xmp",))
        self.assertEqual(result.sha256, expected)

    def test_doi_in_xmp_direct_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "xmp_direct.pdf", xmp=XMP_DOI_DIRECT)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, DOI),))
        self.assertEqual(result.sources, ("xmp",))
        self.assertEqual(result.confidence, "high")

    def test_xmp_creator_seq_digits_not_reported_as_pmid(self):
        # Only <identifier> elements contribute; rdf:li items of
        # dc:creator/dc:title/dc:subject are never scanned.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "xmp_creator.pdf", xmp=XMP_CREATOR_SEQ)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, f"doi:{DOI}"),))
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.sources, ("xmp",))

    def test_xmp_arbitrary_identifier_prefix_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "xmp_prefix.pdf", xmp=XMP_ARBITRARY_PREFIX)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, f"doi:{DOI}"),))
        self.assertEqual(result.confidence, "high")

    def test_xmp_identifier_li_with_pmid_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "xmp_pmid.pdf", xmp=XMP_PMID_LABEL_IN_BAG)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (pmid(PMID, f"PMID: {PMID}"),))
        self.assertEqual(result.confidence, "high")

    def test_doi_in_xmp_prism_element(self):
        # <prism:doi>10.1000/xyz123</prism:doi> — element local name
        # ``doi`` under any namespace prefix is a candidate.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "xmp_prism.pdf", xmp=XMP_DOI_PRISM)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, DOI),))
        self.assertEqual(result.sources, ("xmp",))
        self.assertEqual(result.confidence, "high")

    def test_doi_in_xmp_identifier_resource_attribute(self):
        # <dc:identifier rdf:resource="https://doi.org/10.1000/xyz123"/>
        # — the rdf:resource attribute of the identifier element.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "xmp_resource.pdf", xmp=XMP_DOI_RESOURCE_ATTR)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, f"https://doi.org/{DOI}"),))
        self.assertEqual(result.sources, ("xmp",))
        self.assertEqual(result.confidence, "high")

    def test_xmp_identifier_local_name_case_insensitive(self):
        # Element local-name matching is case-insensitive
        # (``dc:IDENTIFIER`` with an uppercase local name).
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "xmp_case.pdf", xmp=XMP_UPPERCASE_IDENTIFIER)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, f"doi:{DOI}"),))
        self.assertEqual(result.sources, ("xmp",))
        self.assertEqual(result.confidence, "high")


class FirstPageTextTest(unittest.TestCase):
    def test_doi_in_first_page_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(
                Path(tmp) / "doi_text.pdf", texts=("Published under DOI 10.1000/xyz123",)
            )
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, DOI),))
        self.assertEqual(result.sources, ("first_pages",))
        self.assertEqual(result.confidence, "high")

    def test_pmid_in_first_page_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "pmid_text.pdf", texts=("PMID: 28845751",))
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (pmid(PMID, "PMID: 28845751"),))
        self.assertEqual(result.sources, ("first_pages",))
        self.assertEqual(result.confidence, "high")

    def test_scan_page_range_constant_is_fixed(self):
        self.assertEqual(SCAN_PAGE_RANGE, 3)

    def test_identifier_on_last_scanned_page_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            texts = ["", ""]  # pages 0 and 1 empty
            texts.extend([""] * (SCAN_PAGE_RANGE - 2))  # pad to SCAN_PAGE_RANGE
            texts[SCAN_PAGE_RANGE - 1] = "DOI 10.1000/xyz123"
            path = make_pdf(Path(tmp) / "last_page.pdf", pages=SCAN_PAGE_RANGE, texts=texts)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (doi(DOI, DOI),))
        self.assertEqual(result.confidence, "high")

    def test_identifier_beyond_scan_range_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            texts = [""] * (SCAN_PAGE_RANGE + 1)
            texts[SCAN_PAGE_RANGE] = "DOI 10.1000/xyz123"
            path = make_pdf(Path(tmp) / "beyond.pdf", pages=SCAN_PAGE_RANGE + 1, texts=texts)
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, ())
        self.assertEqual(result.confidence, "low")

    def test_manuscript_number_not_reported_as_pmid(self):
        # A bare 6-9 digit run with no PMID label is a manuscript number,
        # a year, ... — never a standalone PMID.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(
                Path(tmp) / "manuscript.pdf", texts=("Manuscript number 20240802",)
            )
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, ())
        self.assertEqual(result.confidence, "low")

    def test_bare_digits_without_label_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "bare.pdf", texts=("28845751",))
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, ())
        self.assertEqual(result.confidence, "low")

    def test_pmid_with_label_space_no_colon(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "pmid_space.pdf", texts=("PMID 28845751",))
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (pmid(PMID, "PMID 28845751"),))
        self.assertEqual(result.confidence, "high")

    def test_pmid_label_across_line_break(self):
        # The label and the digits may sit on separate text lines; any
        # whitespace (including newlines) between them is acceptable.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pmid_wrap.pdf"
            doc = fitz.open()
            page = doc.new_page(width=612, height=792)
            page.insert_text((72, 72), "PMID:")
            page.insert_text((72, 92), "28845751")
            doc.save(str(path))
            doc.close()
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (pmid(PMID, "PMID:\n28845751"),))
        self.assertEqual(result.confidence, "high")

    def test_pubmed_url_in_first_page_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://pubmed.ncbi.nlm.nih.gov/28845751/"
            path = make_pdf(Path(tmp) / "pubmed_url.pdf", texts=(url,))
            result = extract_pdf_identifiers(path)
        self.assertEqual(result.identifiers, (pmid(PMID, url),))
        self.assertEqual(result.confidence, "high")

    def test_doi_in_publisher_url_on_first_page(self):
        # A Wiley /doi/<doi> link is a publisher URL, not a doi.org
        # resolver — extract_identifiers (Task 9) catches the embedded
        # DOI pattern and the PDF layer must not regress it.
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://onlinelibrary.wiley.com/doi/10.1002/anie.202400001"
            path = make_pdf(Path(tmp) / "wiley_url.pdf", texts=(url,))
            result = extract_pdf_identifiers(path)
        self.assertEqual(
            result.identifiers, (doi("10.1002/anie.202400001", "10.1002/anie.202400001"),)
        )
        self.assertEqual(result.sources, ("first_pages",))
        self.assertEqual(result.confidence, "high")


class NoIdentifierTest(unittest.TestCase):
    def test_no_identifier_returns_low_confidence_candidate_without_fabrication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "none.pdf", texts=("Nothing to see here.",))
            result = extract_pdf_identifiers(path)
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(result.identifiers, ())
        self.assertEqual(result.confidence, "low")
        self.assertEqual(result.sources, ())
        self.assertEqual(result.sha256, expected)


class ChannelMergeTest(unittest.TestCase):
    def test_xmp_and_first_page_merge_deduplicated_in_priority_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(
                Path(tmp) / "merge.pdf",
                texts=("PMID: 28845751 DOI 10.1000/xyz123",),
                xmp=XMP_DOI_IN_BAG,
            )
            result = extract_pdf_identifiers(path)
        self.assertEqual(
            result.identifiers,
            (doi(DOI, f"doi:{DOI}"), pmid(PMID, "PMID: 28845751")),
        )
        self.assertEqual(result.sources, ("xmp", "first_pages"))
        self.assertEqual(result.confidence, "high")


class ErrorTest(unittest.TestCase):
    def test_plain_text_file_raises_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.pdf"
            path.write_bytes(b"this is plain text, not a pdf")
            with self.assertRaises(PdfError) as caught:
                extract_pdf_identifiers(path)
        self.assertEqual(caught.exception.path, path)
        self.assertTrue(caught.exception.reason)

    def test_corrupt_pdf_with_header_raises_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corrupt.pdf"
            path.write_bytes(b"%PDF-1.7\n" + b"garbage" * 200)
            with self.assertRaises(PdfError) as caught:
                extract_pdf_identifiers(path)
        self.assertEqual(caught.exception.path, path)
        self.assertTrue(caught.exception.reason)

    def test_missing_file_raises_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.pdf"
            with self.assertRaises(PdfError) as caught:
                extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertTrue(caught.exception.reason)
            self.assertFalse(path.exists())  # nothing was created

    def test_directory_input_raises_structured_error(self):
        # A directory is an invalid input and must surface as a structured
        # PdfError — IsADirectoryError must never leak.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "adir"
            directory.mkdir()
            with self.assertRaises(PdfError) as caught:
                extract_pdf_identifiers(directory)
            self.assertEqual(caught.exception.path, directory)
            self.assertTrue(caught.exception.reason)
            self.assertNotIsInstance(caught.exception, IsADirectoryError)

    def test_permission_denied_wrapped_as_pdf_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locked.pdf"
            path.write_bytes(b"%PDF-1.7\n")
            with mock.patch.object(
                pdf_module,
                "sha256_stream",
                side_effect=PermissionError(13, "Permission denied"),
            ):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertIn("permission", caught.exception.reason.lower())

    def test_read_error_wrapped_as_pdf_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "io_error.pdf"
            path.write_bytes(b"%PDF-1.7\n")
            with mock.patch.object(
                pdf_module,
                "sha256_stream",
                side_effect=OSError(5, "Input/output error"),
            ):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertTrue(caught.exception.reason)

    def test_file_replaced_between_hash_and_parse_raises(self):
        # TOCTOU: if the file is swapped after the first hash, the digest
        # of the first bytes must never be mixed with identifiers of the
        # second bytes — the swap is detected and reported.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "race.pdf"
            make_pdf(path, texts=("Nothing here.",))  # first bytes: no identifiers
            original_sha256 = pdf_module.sha256_stream
            calls = {"n": 0}

            def side_effect(p):
                calls["n"] += 1
                if calls["n"] == 1:
                    return original_sha256(p)
                # second pass: the file has been swapped for a different DOI PDF
                replacement = Path(tmp) / "replacement.pdf"
                make_pdf(replacement, texts=("DOI 10.1000/xyz123",))
                os.replace(replacement, path)
                return original_sha256(p)

            with mock.patch.object(pdf_module, "sha256_stream", side_effect=side_effect):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertIn("changed", caught.exception.reason)

    def test_second_read_failure_wrapped_as_pdf_error(self):
        # If the file disappears between parsing and the verifying re-hash,
        # that failure is also structured — no bare OSError escapes.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gone.pdf"
            make_pdf(path, texts=("DOI 10.1000/xyz123",))
            original_sha256 = pdf_module.sha256_stream
            calls = {"n": 0}

            def side_effect(p):
                calls["n"] += 1
                if calls["n"] == 1:
                    return original_sha256(p)
                raise FileNotFoundError(2, "No such file")

            with mock.patch.object(pdf_module, "sha256_stream", side_effect=side_effect):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertTrue(caught.exception.reason)

    def test_svg_renamed_as_pdf_raises_structured_error(self):
        # PyMuPDF is a multi-format reader: an SVG named ``.pdf`` opens
        # fine and its text layer carries the DOI — without the is_pdf
        # guard this input is wrongly reported high. It must be a
        # structured "not a PDF" error, never a scanned candidate, and
        # the input file must remain untouched.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.svg.pdf"
            path.write_bytes(_svg_with_doi_bytes())
            before = snapshot(path)
            with self.assertRaises(PdfError) as caught:
                extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertIn("not a pdf", caught.exception.reason.lower())
            self.assertEqual(snapshot(path), before)

    def test_png_renamed_as_pdf_raises_structured_error(self):
        # A valid PNG named ``.pdf`` also opens in PyMuPDF; without the
        # is_pdf guard it would silently be a low-confidence candidate.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.png.pdf"
            path.write_bytes(_png_bytes())
            before = snapshot(path)
            with self.assertRaises(PdfError) as caught:
                extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertIn("not a pdf", caught.exception.reason.lower())
            self.assertEqual(snapshot(path), before)

    def test_encrypted_pdf_raises_structured_error(self):
        # An AES-encrypted PDF opens without the password but its content
        # is inaccessible (needs_pass == 1); page access would raise the
        # raw ValueError("document closed or encrypted"). The password
        # requirement is checked before any scan and surfaces as a
        # structured PdfError; the input file stays untouched.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_encrypted_pdf(Path(tmp) / "encrypted.pdf")
            before = snapshot(path)
            with self.assertRaises(PdfError) as caught:
                extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertNotIsInstance(caught.exception, ValueError)
            combined = caught.exception.reason.lower()
            self.assertTrue("password" in combined or "encrypted" in combined)
            self.assertEqual(snapshot(path), before)

    def test_fitz_value_error_during_parse_wrapped_as_pdf_error(self):
        # A ValueError raised inside collection (e.g. "document closed or
        # encrypted") must be wrapped as corrupt — no bare ValueError
        # ever escapes.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "valerr.pdf", texts=("DOI 10.1000/xyz123",))
            with mock.patch.object(
                pdf_module,
                "_collect",
                side_effect=ValueError("document closed or encrypted"),
            ):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertIn("corrupt", caught.exception.reason.lower())

    def test_fitz_runtime_error_during_parse_wrapped_as_pdf_error(self):
        # A RuntimeError raised inside collection (e.g. a corrupt page)
        # must be wrapped as corrupt — no bare RuntimeError escapes.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "rterr.pdf", texts=("DOI 10.1000/xyz123",))
            with mock.patch.object(
                pdf_module, "_collect", side_effect=RuntimeError("cannot open page")
            ):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertIn("corrupt", caught.exception.reason.lower())

    def test_fitz_runtime_error_during_open_wrapped_as_pdf_error(self):
        # A RuntimeError raised by fitz.open (e.g. a broken document)
        # must be wrapped as a structured PdfError — no bare
        # RuntimeError ever escapes.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken_open.pdf"
            path.write_bytes(b"%PDF-1.7\n")
            with mock.patch.object(
                fitz, "open", side_effect=RuntimeError("cannot open broken document")
            ):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertTrue(caught.exception.reason)
            self.assertNotIsInstance(caught.exception, RuntimeError)

    def test_fitz_filedata_error_during_parse_wrapped_as_pdf_error(self):
        # A FileDataError raised while reading content must be wrapped —
        # no raw fitz error escapes.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "fderr.pdf", texts=("DOI 10.1000/xyz123",))
            with mock.patch.object(
                pdf_module, "_collect", side_effect=fitz.FileDataError("cannot open")
            ):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertIn("corrupt", caught.exception.reason.lower())

    def test_own_pdf_error_not_rewrapped_as_corrupt(self):
        # A PdfError raised by the module's own guards (is_pdf /
        # needs_pass) must propagate as-is — never re-wrapped into a
        # "corrupt PDF" PdfError.
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "own.pdf", texts=("DOI 10.1000/xyz123",))
            own = PdfError(path, "boom")
            with mock.patch.object(pdf_module, "_collect", side_effect=own):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertIs(caught.exception, own)


class FormatGuardTest(unittest.TestCase):
    """The document handle is closed even on the rejection paths."""

    def test_document_closed_when_rejecting_non_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opened.pdf"
            path.write_bytes(b"opened by fitz, rejected as non-pdf")
            fake = mock.Mock()
            fake.is_pdf = False
            fake.needs_pass = 0
            with mock.patch.object(fitz, "open", return_value=fake):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            fake.close.assert_called_once_with()

    def test_document_closed_when_rejecting_encrypted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opened.pdf"
            path.write_bytes(b"opened by fitz, rejected as encrypted")
            fake = mock.Mock()
            fake.is_pdf = True
            fake.needs_pass = 1
            with mock.patch.object(fitz, "open", return_value=fake):
                with self.assertRaises(PdfError) as caught:
                    extract_pdf_identifiers(path)
            self.assertEqual(caught.exception.path, path)
            self.assertNotIsInstance(caught.exception, ValueError)
            fake.close.assert_called_once_with()


class ImmutabilityTest(unittest.TestCase):
    def test_input_unchanged_on_success_with_xmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "immutable.pdf", xmp=XMP_DOI_IN_BAG)
            before = snapshot(path)
            extract_pdf_identifiers(path)
            self.assertEqual(snapshot(path), before)

    def test_input_unchanged_on_no_identifier_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "immutable_none.pdf", texts=("No identifiers.",))
            before = snapshot(path)
            extract_pdf_identifiers(path)
            self.assertEqual(snapshot(path), before)

    def test_input_unchanged_on_corrupt_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "immutable_bad.pdf"
            path.write_bytes(b"%PDF-1.7\n" + b"garbage" * 200)
            before = snapshot(path)
            with self.assertRaises(PdfError):
                extract_pdf_identifiers(path)
            self.assertEqual(snapshot(path), before)


if __name__ == "__main__":
    unittest.main()

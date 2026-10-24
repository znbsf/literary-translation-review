"""Synthetic navigation fixtures only; normal body files are deliberately invalid."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ltr.project.catalogue import CatalogueError, locate_chapter
from ltr.project.epub import EpubBook


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fixture.epub"

    def make(self, points=None, nav=None, no_nav=False, declaration=''):
        container = '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'
        if nav is not None:
            resource = '<item id="toc" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            nav_name = "OPS/nav.xhtml"
            nav_text = '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>' + nav + '</ol></nav></body></html>'
            toc = ''
        else:
            resource = '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            nav_name = "OPS/toc.ncx"
            nav_text = '<ncx><navMap>' + (points or '') + '</navMap></ncx>'
            toc = ' toc="toc"'
        if no_nav:
            resource = '';toc = ''
        opf = '<package><metadata/><manifest>' + resource + ''.join(
            f'<item id="s{i}" href="{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(3)) + '</manifest><spine' + toc + '>' + ''.join(
            f'<itemref idref="s{i}"/>' for i in range(3)) + '</spine></package>'
        with zipfile.ZipFile(self.path, "w") as z:
            z.writestr("META-INF/container.xml", container)
            z.writestr("OPS/book.opf", opf)
            if not no_nav:
                z.writestr(nav_name, declaration + nav_text)
            for i in range(3):
                z.writestr(f"OPS/{i}.xhtml", 'THIS IS NOT XML AND MUST NEVER BE READ')

    def point(self, label, href, children=''):
        return f'<navPoint><navLabel><text>{label}</text></navLabel><content src="{href}"/>{children}</navPoint>'

    def test_ncx_range_reads_only_navigation(self):
        self.make(self.point("Target", "0.xhtml") + self.point("Next", "2.xhtml"))
        reads = []
        original = EpubBook.read
        def record(book, name):
            reads.append(name)
            return original(book, name)
        with patch.object(EpubBook, 'read', record):
            result = locate_chapter(self.path, ["Target"])
        self.assertEqual(result['spine_ids'], ['s0', 's1'])
        self.assertEqual(result['confidence'], 'navigation-only')
        self.assertEqual(set(reads), {'META-INF/container.xml', 'OPS/book.opf', 'OPS/toc.ncx'})
        self.assertNotIn('Target', repr(result))

    def test_exact_whitespace_normalized_match(self):
        self.make(self.point(" A  B ", "0.xhtml"))
        self.assertEqual(locate_chapter(self.path, ['A\nB'])['match_count'], 1)
        self.assertEqual(locate_chapter(self.path, ['A'])['confidence'], 'unmatched')

    def test_anchored_ordinal_prefix_allowed(self):
        for prefix in ('第一話　', '第12话 ', '第１２章', '第十二話 '):
            with self.subTest(prefix=prefix):
                self.make(self.point(prefix + 'Target', '0.xhtml'))
                self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'navigation-only')

    def test_arbitrary_substrings_and_suffixes_not_matched(self):
        for label in ('Extra Target', 'Target Extra', '第一話 Target Extra', 'Part 1 Target'):
            with self.subTest(label=label):
                self.make(self.point(label, '0.xhtml'))
                self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'unmatched')

    def test_exact_and_ordinal_matches_remain_ambiguous(self):
        self.make(self.point('Target', '0.xhtml') + self.point('第一話 Target', '1.xhtml'))
        result = locate_chapter(self.path, ['Target'])
        self.assertEqual(result['match_count'], 2)
        self.assertEqual(result['confidence'], 'ambiguous')
        self.assertEqual(result['spine_ids'], [])

    def test_duplicate_label_ambiguous(self):
        self.make(self.point("Target", "0.xhtml") + self.point("Target", "2.xhtml"))
        result = locate_chapter(self.path, ['Target'])
        self.assertEqual(result['match_count'], 2)
        self.assertEqual(result['confidence'], 'ambiguous')
        self.assertEqual(result['spine_ids'], [])

    def test_same_spine_fragments_require_selection(self):
        self.make(self.point("Target", "0.xhtml#a") + self.point("Next", "0.xhtml#b"))
        result = locate_chapter(self.path, ['Target'])
        self.assertEqual(result['confidence'], 'needs-fragment-selection')
        self.assertEqual(result['spine_ids'], ['s0'])
        self.assertEqual(result['start_href'], 'OPS/0.xhtml#a')
        self.assertEqual(result['end_href'], 'OPS/0.xhtml#b')

    def test_cross_spine_end_fragment_included_as_candidate(self):
        self.make(self.point("Target", "0.xhtml") + self.point("Next", "2.xhtml#b"))
        result = locate_chapter(self.path, ['Target'])
        self.assertEqual(result['confidence'], 'needs-fragment-selection')
        self.assertEqual(result['spine_ids'], ['s0', 's1', 's2'])

    def test_same_spine_without_fragments_ambiguous(self):
        self.make(self.point("Target", "0.xhtml") + self.point("Next", "0.xhtml"))
        self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'ambiguous')

    def test_nested_target_uses_ancestor_boundary(self):
        self.make(self.point('Part', '0.xhtml', self.point('Target', '1.xhtml')) + self.point('Next', '2.xhtml'))
        self.assertEqual(locate_chapter(self.path, ['Target'])['spine_ids'], ['s1'])

    def test_epub3_nav_range(self):
        self.make(nav='<li><a href="0.xhtml">Target</a></li><li><a href="2.xhtml">Next</a></li>')
        result = locate_chapter(self.path, ['Target'])
        self.assertEqual(result['confidence'], 'navigation-only')
        self.assertEqual(result['spine_ids'], ['s0', 's1'])

    def test_nav_heading_without_anchor_cannot_bound_range(self):
        self.make(nav='<li><a href="0.xhtml">Target</a></li><li><span>Next</span></li>')
        self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'ambiguous')

    def test_absent_navigation_unmatched(self):
        self.make(no_nav=True)
        self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'unmatched')

    def test_nonspine_target_ambiguous(self):
        self.make(self.point('Target', 'outside.xhtml'))
        self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'ambiguous')

    def test_reversed_navigation_ambiguous(self):
        self.make(self.point('Target', '2.xhtml') + self.point('Next', '1.xhtml'))
        self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'ambiguous')

    def test_unsafe_href_fixed_error(self):
        for href in ('https://example.invalid/x', '../../outside.xhtml', '%2e%2e/%2e%2e/outside.xhtml'):
            with self.subTest(href=href):
                self.make(self.point('Target', href))
                with self.assertRaises(CatalogueError) as error:
                    locate_chapter(self.path, ['Target'])
                self.assertIn(str(error.exception), ('CATALOGUE_UNSAFE_HREF', 'EPUB_UNSAFE_PATH'))

    def test_invalid_label_fixed_error(self):
        with self.assertRaisesRegex(CatalogueError, '^CATALOGUE_INVALID_LABELS$'):
            locate_chapter(self.path, [])

    def test_exact_standard_ncx_doctype_is_inert(self):
        declaration = '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">'
        self.make(self.point('Target', '0.xhtml'), declaration=declaration)
        self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'navigation-only')

    def test_standard_ncx_with_internal_subset_rejected(self):
        declaration = '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd" [<!ENTITY x "bad">]>'
        self.make(self.point('Target', '0.xhtml'), declaration=declaration)
        with self.assertRaisesRegex(CatalogueError, '^CATALOGUE_UNSAFE_XML$'):
            locate_chapter(self.path, ['Target'])

    def test_forged_ncx_dtd_url_rejected(self):
        declaration = '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "https://example.invalid/ncx-2005-1.dtd">'
        self.make(self.point('Target', '0.xhtml'), declaration=declaration)
        with self.assertRaisesRegex(CatalogueError, '^CATALOGUE_UNSAFE_XML$'):
            locate_chapter(self.path, ['Target'])

    def test_exact_html5_navigation_doctype_is_inert(self):
        self.make(nav='<li><a href="0.xhtml">Target</a></li>', declaration='<!DOCTYPE html>')
        self.assertEqual(locate_chapter(self.path, ['Target'])['confidence'], 'navigation-only')

    def test_html5_external_or_internal_declaration_rejected(self):
        for declaration in ('<!DOCTYPE html SYSTEM "https://example.invalid/dtd">', '<!DOCTYPE html [ ]>'):
            with self.subTest(declaration=declaration):
                self.make(nav='<li><a href="0.xhtml">Target</a></li>', declaration=declaration)
                with self.assertRaisesRegex(CatalogueError, '^CATALOGUE_UNSAFE_XML$'):
                    locate_chapter(self.path, ['Target'])


if __name__ == '__main__':
    unittest.main()

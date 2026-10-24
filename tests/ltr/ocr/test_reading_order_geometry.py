import unittest

from ltr.ocr import reading_order_geometry as geometry

class GeometryPortTests(unittest.TestCase):
    def test_small_page_unclassified_preserves_exact_raw_refs(self):
        lines=[{"words":[{"x":1,"y":2,"w":3,"h":4},{"x":8,"y":9,"w":2,"h":3}]}, {"words":[]}]
        result=geometry.word_order(lines,100,200)
        self.assertEqual(result["layout"],"unclassified")
        self.assertEqual(result["word_order"],[[0,0],[0,1]])
        self.assertEqual((result["raw_line_count"],result["raw_word_count"]),(2,2))
        self.assertTrue(result["coverage_exact_once"])

    def test_empty_page(self):
        result=geometry.word_order([],100,200)
        self.assertEqual(result["layout"],"unclassified")
        self.assertEqual(result["word_order"],[])
        self.assertEqual(result["raw_word_count"],0)

    def test_cross_band_lines_split_by_word_midpoint(self):
        lines=[]
        for x in (10,10,40,40):
            lines.append({"words":[{"x":x,"y":20,"w":5,"h":25},{"x":x,"y":140,"w":5,"h":25}]})
        result=geometry.word_order(lines,200,200)
        self.assertEqual(result["layout"],"vertical_multi_band")
        self.assertEqual(len(result["bands"]),2)
        self.assertEqual(result["raw_word_count"],8)
        self.assertEqual(len(result["segments"]),8)
        for line_no in range(4):
            self.assertEqual({s["band"] for s in result["segments"] if s["source_line"]==line_no},{0,1})
        for band_no in range(2):
            self.assertEqual({s["column"] for s in result["segments"] if s["band"]==band_no},{0,1})
        self.assertEqual(sorted(map(tuple,result["word_order"])),
                         [(li,wi) for li,line in enumerate(lines) for wi,_ in enumerate(line["words"])])
        self.assertTrue(all(seg["word_refs"] for seg in result["segments"]))

    def test_stable_y_order_for_equal_y_keeps_source_word_order(self):
        lines=[]
        for i in range(4):
            lines.append({"words":[{"x":i*10,"y":20,"w":5,"h":12},{"x":i*10,"y":20,"w":5,"h":12},{"x":i*10,"y":40,"w":5,"h":12}]})
        result=geometry.word_order(lines,100,100)
        self.assertNotEqual(result["layout"],"unclassified")
        for seg in result["segments"]:
            refs=seg["word_refs"]
            self.assertEqual(refs,sorted(refs,key=lambda r:(lines[r[0]]["words"][r[1]]["y"],r[1])))
            tied=[r[1] for r in refs if lines[r[0]]["words"][r[1]]["y"]==20]
            if tied: self.assertEqual(tied,[0,1])

    def test_bool_nonfinite_and_out_of_bounds_are_rejected(self):
        for word in ({"x":True,"y":0,"w":2,"h":2},
                     {"x":float("nan"),"y":0,"w":2,"h":2},
                     {"x":99,"y":0,"w":2,"h":2}):
            with self.subTest(word=word):
                with self.assertRaises(geometry.GeometryError):
                    geometry.word_order([{"words":[word]}],100,100)

if __name__=="__main__": unittest.main()

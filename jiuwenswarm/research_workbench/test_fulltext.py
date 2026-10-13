import json
import tempfile
import unittest
from pathlib import Path
from .fulltext import build,chunks,sha,search


class FulltextTest(unittest.TestCase):
    def test_unicode_offsets_limits_and_coverage(self):
        text=('中文🙂 source\n'*90)+'end'
        result=list(chunks(text,100,15));covered=set()
        for c in result:
            self.assertEqual(text[c['start_char']:c['end_char']],c['text'])
            self.assertLessEqual(c['bytes'],100)
            covered.update(range(c['start_char'],c['end_char']))
        self.assertEqual(len(covered),len(text))
        with self.assertRaises(ValueError):list(chunks(text,10,10))

    def test_tampered_page_and_source_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pdf=root/'fixture.pdf';pdf.write_bytes(b'fixture bytes only')
            extracted=pdf.with_suffix('.extracted.txt');extracted.write_text('\n=== PDF PAGE 1 ===\nEvidence text',encoding='utf-8')
            idx=root/'index.json';idx.write_text(json.dumps([{'pdf':'fixture.pdf','sha256':sha(pdf.read_bytes()),'pages':[{'page':1,'text_sha256':sha(b'Evidence text'),'chars':13}]}]),encoding='utf-8')
            result=build(root,idx)
            self.assertEqual(len(result['chunks']),1)
            self.assertTrue(search(result,'Evidence'))
            self.assertIsNone(result['documents'][0]['source_group'])
            extracted.write_text('\n=== PDF PAGE 1 ===\nChanged text',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'page hash'):build(root,idx)
            pdf.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'PDF hash'):build(root,idx)

    def test_tampered_chunk_blocked(self):
        with self.assertRaisesRegex(ValueError,'Chunk hash'):search({'chunks':[{'text':'fake','sha256':'wrong'}]},'fake')

if __name__=='__main__':unittest.main()

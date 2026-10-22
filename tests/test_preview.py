import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aelix_acp.preview import bundle_run


class PreviewTests(unittest.TestCase):
    def test_assets_are_embedded_and_source_record_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'workspace/runs').mkdir(parents=True)
            (root/'workspace/assets').mkdir()
            (root/'workspace/assets/workspace.css').write_text('body {color:navy}')
            (root/'workspace/assets/plotly.min.js').write_text('window.Plotly={newPlot(){}};')
            source = root/'workspace/runs/fixture.html'
            original = ('<html data-plotly="../assets/plotly.min.js"><head>'
                        '<link rel="stylesheet" href="../assets/workspace.css?v=1">'
                        '<script src="../assets/workspace.js"></script></head><body>'
                        '<div class="plotly-chart" data-fig="fig1"></div>'
                        '<script type="application/json" id="fig1">{"data":[]}</script>'
                        '<a href="../index.html">original</a></body></html>')
            source.write_text(original)
            output = bundle_run(root,'fixture').read_text()
            self.assertEqual(source.read_text(),original)
            self.assertIn('<style>body {color:navy}</style>',output)
            self.assertIn('Plotly.newPlot',output)
            self.assertLess(len(output.encode()), 1_000_000)
            self.assertNotIn('<script src=',output)
            self.assertIn(hashlib.sha256(original.encode()).hexdigest(),output)
            self.assertIn((root/'workspace/index.html').resolve().as_uri(),output)

    def test_preview_cannot_embed_files_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'workspace/runs').mkdir(parents=True)
            (root/'workspace/runs/fixture.html').write_text(
                '<link rel="stylesheet" href="../../../../outside.css">')
            with self.assertRaisesRegex(ValueError,'escapes'):
                bundle_run(root,'fixture')

    def test_remote_assets_are_not_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'workspace/runs').mkdir(parents=True)
            (root/'workspace/runs/fixture.html').write_text(
                '<link rel="stylesheet" href="https://example.com/style.css">')
            with self.assertRaisesRegex(ValueError,'local'):
                bundle_run(root,'fixture')

"""Self-contained snapshot of a generated Analytics run page for AionUi.

AionUi's HTML preview does not resolve the report's relative asset URLs. Bundle
only project-local assets into a NEW page, preserving the source report bytes.
No server is started and no remote assets are fetched.
"""
import argparse
import base64
import hashlib
import html
import gzip
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def bundle_run(project, run_id):
    project = Path(project).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError("Invalid run ID")
    source = project / "workspace" / "runs" / f"{run_id}.html"
    if not source.resolve().is_relative_to(project):
        raise ValueError("Report source escapes the analysis project")
    original = source.read_bytes()
    page = original.decode("utf-8")

    def local_path(url):
        parsed = urlsplit(html.unescape(url))
        if parsed.scheme or parsed.netloc:
            raise ValueError("Only local report assets can be bundled")
        path = (source.parent / unquote(parsed.path)).resolve()
        if not path.is_relative_to(project):
            raise ValueError("Report asset escapes the analysis project")
        return path

    def read_asset(url):
        path = local_path(url)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Report asset exceeds the preview size limit")
        return path.read_bytes()

    def stylesheet(match):
        css = read_asset(match[1]).decode("utf-8").replace("</style", "<\\/style")
        if re.search(r"url\s*\(|@import", css, re.I):
            raise ValueError("CSS with external resource references is not supported")
        return f"<style>{css}</style>"

    page = re.sub(r'<link\s+rel="stylesheet"\s+href="([^"]+)"\s*/?>', stylesheet, page)
    # Do not carry live-reload, navigation, or dynamic external-script loading
    # into a static snapshot. Chart JSON remains byte-for-byte in the document.
    page = re.sub(r'<script\b[^>]*\bsrc="[^"]+"[^>]*>\s*</script>', "", page)
    page = re.sub(r'<label class="live">.*?</label>', "", page, flags=re.S)
    page = re.sub(r'\sdata-plotly="[^"]*"', "", page)

    def image(match):
        tag, url = match[1], match[2]
        if url.startswith("data:"):
            return match[0]
        mime = mimetypes.guess_type(local_path(url).name)[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            raise ValueError("Non-image resource in image tag")
        data = base64.b64encode(read_asset(url)).decode()
        return f'{tag}src="data:{mime};base64,{data}"'

    page = re.sub(r'(<img\b[^>]*?)src="([^"]+)"', image, page)

    def link(match):
        url = match[1]
        if url.startswith("#") or urlsplit(html.unescape(url)).scheme:
            return match[0]
        parsed = urlsplit(html.unescape(url))
        target = local_path(url).as_uri()
        if parsed.fragment:
            target += "#" + parsed.fragment
        return f'href="{html.escape(target, quote=True)}"'

    page = re.sub(r'href="([^"]+)"', link, page)
    digest = hashlib.sha256(original).hexdigest()
    page = page.replace("</head>", f'<meta name="aelix-acp-source-sha256" content="{digest}"></head>')
    banner = ('<div style="padding:12px;background:#eef4ff;color:#173052;font:14px system-ui">'
              'AionUi preview snapshot · 원본 실행 기록과 코드 해시는 아래에 보존됩니다. '
              '전체 보고서 탐색은 원본 workspace/index.html을 사용하세요.</div>')
    page = re.sub(r'(<body\b[^>]*>)', lambda m: m[1] + banner, page, count=1)
    if 'class="plotly-chart"' in page:
        for figure in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', page, re.S):
            for trace in json.loads(figure).get("data", []):
                if trace.get("type", "scatter") not in {"bar", "pie", "scatter"}:
                    raise ValueError("AionUi compact preview supports Plotly bar, pie and scatter only")
        packed = (Path(__file__).parent / "assets/plotly-basic-3.7.0.min.js.gz").read_bytes()
        expected = "c23b03591a6bdad0bd0f47a0c5c52305a5519812b73ce254be65cd841553c9c2"
        if hashlib.sha256(gzip.decompress(packed)).hexdigest() != expected:
            raise ValueError("Bundled Plotly asset failed its integrity check")
        encoded = base64.b64encode(packed).decode("ascii")
        hydration = '''<script>
(async function() {
const packed = Uint8Array.from(atob("__PLOTLY_GZIP__"), c => c.charCodeAt(0));
const stream = new Blob([packed]).stream().pipeThrough(new DecompressionStream('gzip'));
const script = document.createElement('script');
script.textContent = await new Response(stream).text();
document.head.appendChild(script);
for (const el of document.querySelectorAll('.plotly-chart[data-fig]')) {
  const fig = JSON.parse(document.getElementById(el.dataset.fig).textContent);
  await Plotly.newPlot(el, fig.data || [], fig.layout || {}, {responsive:true,displaylogo:false});
}
})().catch(error => {
  for (const el of document.querySelectorAll('.plotly-chart')) el.textContent = 'Chart unavailable: ' + error.message;
});
</script>'''.replace("__PLOTLY_GZIP__", encoded)
        page = page.replace("</body>", hydration + "</body>")
    if len(page.encode("utf-8")) > 1_000_000:
        raise ValueError("Bundled preview exceeds this PoC's compact 1MB snapshot budget")
    output = project / "workspace/aion-preview" / f"{run_id}.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("run_id")
    args = parser.parse_args()
    print(bundle_run(args.project, args.run_id))


if __name__ == "__main__":
    main()

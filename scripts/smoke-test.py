"""Check installed assets and the sample's spoken figure reference."""

from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from aloud.extract_doc import load_document


def main():
    print(f"ALOUD {version('aloud-macos')}")
    package = files("aloud")
    for asset in (
        "web/index.html", "web/app.js", "web/help.html", "web/delight.js",
        "web/vendor/katex/katex.min.js", "web/vendor/katex/fonts/KaTeX_Main-Regular.woff2",
        "vendor/sre/sre.js", "assets/ALOUD.icns",
    ):
        assert (package / asset).is_file(), f"Missing packaged asset {asset}"
    sample = Path(__file__).resolve().parents[1] / "examples" / "reading-in-sync.pdf"
    with TemporaryDirectory() as cache:
        doc = load_document(sample, cache_dir=Path(cache))
    figure = next(
        (doc["paras"][i]["figure"] for para in doc["paras"]
         for start, end, i, panel in para.get("figure_refs", [])
         if panel == "b" and para["text"][start:end] == "Figure 1b"), None,
    )
    assert figure and figure["crop"][0] == 1 and "b" in figure["panels"], "Missing Figure 1b on page 2"
    assert doc["units"], "The sample has no readable passages"
    print("Packaged assets, PDF text, cross-page figure and panel outline passed.")


if __name__ == "__main__":
    main()

"""Original synthetic sentences, unrelated to any private reference work."""
from pathlib import Path
import zipfile
from .epub import import_items
from .storage import private_root, StorageError, create_project
from .preview import render


def demo(root: Path) -> dict:
    root = private_root(root)
    if (root / "project.json").exists() or (root / "synthetic.epub").exists():
        raise StorageError("project_already_exists")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "synthetic.epub"
    entries = {
        "META-INF/container.xml": '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        "OPS/book.opf": '<package><metadata><title>Synthetic only</title><language>ja</language></metadata><manifest><item id="scene" href="scene.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="scene"/></spine></package>',
        "OPS/scene.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>ミラは箱を開けなかった。</p><p>窓のそばに<ruby>猫<rt>ねこ</rt></ruby>がいた。</p><p>「隊長」と彼女は呼んだ。<br/>それ以上は何も言わなかった。</p></body></html>',
    }
    with zipfile.ZipFile(path, "x") as z:
        for name, content in entries.items():
            z.writestr(name, content.encode("utf-8"))
    project = create_project(root, import_items(path, ["scene"]))
    render(root, project)
    return project

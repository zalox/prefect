# from pdoct examples on GH, render Markdown files from pdoc-generated HTML
# see the repo https://github.com/mitmproxy/pdoc/tree/main/examples/mkdocs
# for example how it uses jinja2 to not include bootstrap
# will still need to find add the new files to the mint.json file and put in a folder for the current version I guess

import shutil
from pathlib import Path

from pdoc import pdoc, render

here = Path(__file__).parent
out = here / "docs" / "api"
if out.exists():
    shutil.rmtree(out)

# Render parts of pdoc's documentation into docs/api...
render.configure(template_directory=here / "pdoc-template")
pdoc("pdoc", "!pdoc.", "pdoc.doc", output_directory=out)

# ...and rename the .html files to .md so that mkdocs picks them up!
for f in out.glob("**/*.html"):
    f.rename(f.with_suffix(".md"))

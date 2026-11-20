# Development

Tests:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements_test.txt   # Scripts/ instead of bin/ on Windows
.venv/bin/python -m pytest
```

Help pages (MkDocs Material, English with a German translation in `*.de.md` next to each page; diagrams in
`docs/assets/diagrams`, a German copy of each as `*.de.svg`):

```bash
.venv/bin/python -m pip install -r requirements_docs.txt
.venv/bin/python -m mkdocs serve
```

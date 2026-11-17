"""Extract readable file content; unsupported or damaged files keep their originals."""

from html.parser import HTMLParser
from io import BytesIO
import mimetypes
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile
from PIL import Image


MAX_TEXT = 16 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".jsonl",
                 ".yaml", ".yml", ".toml", ".ini", ".xml", ".html", ".htm", ".py", ".js",
                 ".ts", ".tsx", ".jsx", ".c", ".cpp", ".h", ".rs", ".go", ".java", ".sh", ".sql"}


class ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def office_text(raw, suffix):
    with ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
            raise ValueError("document_expansion_limit")

        def xml(name):
            return ET.fromstring(archive.read(name))

        if suffix == ".docx":
            root = xml("word/document.xml")
            return "\n".join("".join(p.itertext()) for p in root.iter()
                             if p.tag.endswith("}p"))
        if suffix == ".pptx":
            names = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.search(r"(\d+)\.xml$", n)[1]))
            return "\n\n".join("\n".join(e.text or "" for e in xml(n).iter() if e.tag.endswith("}t"))
                                 for n in names)
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        strings = (["".join(e.itertext()) for e in xml("xl/sharedStrings.xml")]
                   if "xl/sharedStrings.xml" in archive.namelist() else [])
        sheets = []
        for name in sorted(n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            rows = []
            for row in xml(name).findall(".//s:row", ns):
                cells = []
                for cell in row.findall("s:c", ns):
                    value = cell.findtext("s:v", "", ns)
                    if cell.get("t") == "s":
                        value = strings[int(value)]
                    elif cell.get("t") == "inlineStr":
                        value = "".join(cell.find("s:is", ns).itertext())
                    cells.append(f'{cell.get("r", "")}: {value}')
                rows.append("\t".join(cells))
            sheets.append(name + "\n" + "\n".join(rows))
        return "\n\n".join(sheets)


def extract_file(name, raw):
    """Return text, MIME and parse state, without executing file content."""
    suffix = Path(name).suffix.lower()
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if mime.split("/")[0] in {"image", "audio", "video"} and suffix != ".svg" and raw:
        try:
            if mime.startswith("image/"):
                with Image.open(BytesIO(raw)) as image:
                    image.verify()
            else:
                from ..media import probe_duration
                probe_duration(raw, mime)
            return None, mime, "native_media"
        except (OSError, ValueError, Image.DecompressionBombError, subprocess.SubprocessError):
            return None, mime, "parse_failed"
    try:
        if suffix == ".pdf" or raw.startswith(b"%PDF-"):
            mime = "application/pdf"
            tool = shutil.which("pdftotext")
            if not tool:
                return None, mime, "parser_unavailable"
            with TemporaryDirectory(prefix="memu-document-") as directory:
                source, output = Path(directory) / "input.pdf", Path(directory) / "body.txt"
                source.write_bytes(raw)
                subprocess.run([tool, "-enc", "UTF-8", str(source), str(output)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                if output.stat().st_size > MAX_TEXT:
                    return None, mime, "text_too_large"
                text = output.read_text()
        elif suffix in {".docx", ".xlsx", ".pptx"}:
            text = office_text(raw, suffix)
        else:
            codecs = (["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig"])
            if suffix in TEXT_SUFFIXES:
                codecs.append("gb18030")
            text = None
            for codec in codecs:
                try:
                    text = raw.decode(codec)
                    break
                except UnicodeError:
                    continue
            if text is None or any(ord(c) < 32 and c not in "\r\n\t\f" for c in text):
                return None, mime, "unsupported_format"
            if suffix in {".html", ".htm"}:
                parser = ReadableHTML()
                parser.feed(text)
                text = "".join(parser.parts)
            if mime == "application/octet-stream":
                mime = "text/plain"
        if len(text) > MAX_TEXT:
            return None, mime, "text_too_large"
        return text, mime, "text_extracted" if text.strip() else "no_text"
    except (OSError, ValueError, KeyError, IndexError, AttributeError, ET.ParseError, BadZipFile,
            RuntimeError, subprocess.SubprocessError):
        return None, mime, "parse_failed"

"""Private attachments for Scenario Runtime; no main-chat attachment or loop state."""

from __future__ import annotations

import io
import sqlite3
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader


MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT = 20_000
UPLOAD_TTL = 24 * 60 * 60
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
DOCUMENT_TYPES = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".txt": "text/plain", ".md": "text/markdown"}


@dataclass(frozen=True)
class ScenarioAttachment:
    attachment_id: str
    owner_id: str
    scenario_id: str
    name: str
    media_type: str
    kind: str
    size_bytes: int
    path: Path
    extracted_text: str
    expires_at: float
    used: bool


def _image_signature(media_type: str, data: bytes) -> bool:
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _document_text(extension: str, data: bytes) -> str:
    if extension in {".txt", ".md"}:
        try:
            value = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文档需要使用 UTF-8 编码") from exc
    elif extension == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("PDF 文件内容无效")
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if len(reader.pages) > 30:
                raise ValueError("PDF 最多支持 30 页")
            value = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法读取 PDF 文本") from exc
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > 2000 or sum(member.file_size for member in members) > 20 * 1024 * 1024:
                    raise ValueError("Word 文档内容过大")
                xml = archive.read("word/document.xml")
            root = ElementTree.fromstring(xml)
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            value = "\n".join(
                "".join(node.text or "" for node in paragraph.iter(namespace + "t"))
                for paragraph in root.iter(namespace + "p")
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法读取 Word 文档") from exc
    value = value.strip()
    if not value:
        raise ValueError("文档没有可读取的文字；扫描版 PDF 请先转成可选中文字的 PDF")
    return value[:MAX_TEXT]


class ScenarioAttachmentStore:
    def __init__(self, database_path: str | Path, storage_dir: str | Path) -> None:
        self.database_path = Path(database_path)
        self.storage_dir = Path(storage_dir)
        self.max_bytes = MAX_BYTES
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS scenario_attachments (
                attachment_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scenario_id TEXT NOT NULL,
                name TEXT NOT NULL, media_type TEXT NOT NULL, kind TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, file_path TEXT NOT NULL, extracted_text TEXT NOT NULL,
                expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def upload(self, owner_id: str, scenario_id: str, filename: str, media_type: str, data: bytes) -> ScenarioAttachment:
        name = Path(str(filename or "").replace("\\", "/")).name[:255]
        extension = Path(name).suffix.lower()
        expected = IMAGE_TYPES.get(extension) or DOCUMENT_TYPES.get(extension)
        if not expected or media_type.lower().strip() not in {expected, "application/octet-stream"}:
            raise ValueError("仅支持 PNG、JPEG、WebP、GIF、PDF、DOCX、TXT、MD 文件")
        if not data or len(data) > self.max_bytes:
            raise ValueError("附件为空或超过 10 MB")
        kind = "image" if extension in IMAGE_TYPES else "document"
        if kind == "image" and not _image_signature(expected, data):
            raise ValueError("图片内容与格式不符")
        extracted = "" if kind == "image" else _document_text(extension, data)
        identifier = f"scenario-att-{uuid.uuid4().hex}"
        target = self.storage_dir / f"{identifier}{extension}"
        expiry = time.time() + UPLOAD_TTL
        target.write_bytes(data)
        try:
            with self._connect() as db:
                db.execute("""INSERT INTO scenario_attachments
                    (attachment_id,owner_id,scenario_id,name,media_type,kind,size_bytes,file_path,extracted_text,expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (identifier, owner_id, scenario_id, name, expected, kind, len(data), str(target), extracted, expiry))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return ScenarioAttachment(identifier, owner_id, scenario_id, name, expected, kind, len(data), target, extracted, expiry, False)

    def get(self, owner_id: str, scenario_id: str, attachment_id: str) -> ScenarioAttachment | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM scenario_attachments WHERE attachment_id=? AND owner_id=? AND scenario_id=?",
                             (attachment_id, owner_id, scenario_id)).fetchone()
        # Once a file is attached to a turn it remains readable for that
        # scenario's history; only unsubmitted uploads expire.
        if row is None or (not row["used"] and row["expires_at"] <= time.time()):
            return None
        path = Path(row["file_path"])
        if not path.is_file() or path.is_symlink():
            return None
        return ScenarioAttachment(row["attachment_id"], row["owner_id"], row["scenario_id"], row["name"],
                                  row["media_type"], row["kind"], row["size_bytes"], path,
                                  row["extracted_text"], row["expires_at"], bool(row["used"]))

    def mark_used(self, owner_id: str, scenario_id: str, attachment_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE scenario_attachments SET used=1 WHERE owner_id=? AND scenario_id=? AND attachment_id=?",
                       (owner_id, scenario_id, attachment_id))

    def cleanup_expired(self) -> int:
        with self._connect() as db:
            rows = db.execute("SELECT file_path FROM scenario_attachments WHERE used=0 AND expires_at<=?", (time.time(),)).fetchall()
            db.execute("DELETE FROM scenario_attachments WHERE used=0 AND expires_at<=?", (time.time(),))
        for row in rows:
            Path(row["file_path"]).unlink(missing_ok=True)
        return len(rows)

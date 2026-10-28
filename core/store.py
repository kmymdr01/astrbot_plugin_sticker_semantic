"""本地表情包库：文件去重、SQLite 元数据、使用统计。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import aiosqlite

ALLOWED_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
)

STATUS_READY = "ready"
STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"


@dataclass(slots=True)
class Sticker:
    id: str
    filename: str
    sha256: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    intensity: int = 3
    usage: str = "both"
    safe: bool = True
    status: str = STATUS_PENDING
    confirmed: bool = True
    source: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0
    path: str = ""

    @property
    def ready(self) -> bool:
        return self.confirmed and self.status == STATUS_READY and self.safe

    @property
    def english_tags(self) -> str:
        return ", ".join(self.tags)


class StickerStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.stickers_dir = self.data_dir / "stickers"
        self.db_path = self.data_dir / "stickers.db"
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        self.stickers_dir.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stickers (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                keywords TEXT NOT NULL DEFAULT '[]',
                intensity INTEGER NOT NULL DEFAULT 3,
                usage TEXT NOT NULL DEFAULT 'both',
                safe INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                confirmed INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                last_used_at REAL NOT NULL DEFAULT 0,
                use_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stickers_status ON stickers(status)"
        )
        cursor = await self._conn.execute("PRAGMA table_info(stickers)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        if "confirmed" not in columns:
            # 旧库升级：已有表情包都视为已人工确认，新入库的才进入“新加入”。
            await self._conn.execute(
                "ALTER TABLE stickers ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1"
            )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            try:
                await self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("StickerStore not initialized")
        return self._conn

    def _row_to_sticker(self, row: aiosqlite.Row) -> Sticker:
        def _load_list(value: object) -> list[str]:
            if isinstance(value, str) and value:
                try:
                    data = json.loads(value)
                    if isinstance(data, list):
                        return [str(x) for x in data if str(x).strip()]
                except Exception:
                    pass
            return []

        filename = str(row["filename"])
        return Sticker(
            id=str(row["id"]),
            filename=filename,
            sha256=str(row["sha256"]),
            description=str(row["description"] or ""),
            tags=_load_list(row["tags"]),
            keywords=_load_list(row["keywords"]),
            intensity=int(row["intensity"] or 3),
            usage=str(row["usage"] or "both"),
            safe=bool(row["safe"]),
            status=str(row["status"] or STATUS_PENDING),
            confirmed=bool(row["confirmed"]),
            source=str(row["source"] or ""),
            created_at=float(row["created_at"] or 0),
            last_used_at=float(row["last_used_at"] or 0),
            use_count=int(row["use_count"] or 0),
            path=str(self.stickers_dir / filename),
        )

    async def get(self, sticker_id: str) -> Sticker | None:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM stickers WHERE id = ?", (str(sticker_id),)
            )
            row = await cursor.fetchone()
        return self._row_to_sticker(row) if row else None

    async def get_by_sha(self, sha256: str) -> Sticker | None:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM stickers WHERE sha256 = ?", (str(sha256),)
            )
            row = await cursor.fetchone()
        return self._row_to_sticker(row) if row else None

    async def list_all(self, status: str | None = None) -> list[Sticker]:
        async with self._lock:
            if status:
                cursor = await self.conn.execute(
                    "SELECT * FROM stickers WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                )
            else:
                cursor = await self.conn.execute(
                    "SELECT * FROM stickers ORDER BY created_at DESC"
                )
            rows = await cursor.fetchall()
        return [self._row_to_sticker(r) for r in rows]

    async def list_ready(self, confirmed_only: bool = False) -> list[Sticker]:
        stickers = await self.list_all(STATUS_READY)
        if confirmed_only:
            return [s for s in stickers if s.confirmed]
        return stickers

    async def count(self) -> int:
        async with self._lock:
            cursor = await self.conn.execute("SELECT COUNT(*) FROM stickers")
            row = await cursor.fetchone()
        return int(row[0] if row else 0)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def add_file(
        self,
        source_path: str | Path,
        *,
        source: str = "import",
        original_name: str = "",
        confirmed: bool = False,
    ) -> tuple[Sticker | None, bool]:
        """复制图片进插件目录并写入 pending 记录。

        Args:
            confirmed: 旧库迁移/明确人工导入可传 True；自动学习/网页上传等新收录默认 False。

        Returns:
            (sticker, created)。已存在相同 sha256 时返回已有记录且 created=False。
        """
        src = Path(source_path)
        if not src.is_file():
            return None, False
        ext = src.suffix.lower()
        if ext not in ALLOWED_EXTS:
            return None, False

        try:
            sha = await asyncio.to_thread(self._hash_file, src)
        except OSError:
            return None, False

        existing = await self.get_by_sha(sha)
        if existing:
            return existing, False

        sticker_id = sha[:16]
        filename = f"{sticker_id}{ext}"
        dest = self.stickers_dir / filename
        try:
            if src.resolve() != dest.resolve():
                await asyncio.to_thread(shutil.copyfile, src, dest)
        except OSError:
            return None, False

        now = time.time()
        name = original_name or src.name
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO stickers
                (id, filename, sha256, description, tags, keywords, intensity,
                 usage, safe, status, confirmed, source, created_at)
                VALUES (?, ?, ?, ?, '[]', '[]', 3, 'both', 1, 'pending', ?, ?, ?)
                """,
                (sticker_id, filename, sha, name, 1 if confirmed else 0, source, now),
            )
            await self.conn.commit()
            cursor = await self.conn.execute(
                "SELECT * FROM stickers WHERE id = ?", (sticker_id,)
            )
            row = await cursor.fetchone()
        return (self._row_to_sticker(row) if row else None), True

    async def update_classification(
        self,
        sticker_id: str,
        *,
        description: str,
        tags: Iterable[str],
        keywords: Iterable[str],
        intensity: int,
        usage: str,
        safe: bool,
        status: str = STATUS_READY,
    ) -> None:
        usage = usage if usage in ("reaction", "augment", "both") else "both"
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE stickers
                SET description = ?, tags = ?, keywords = ?, intensity = ?,
                    usage = ?, safe = ?, status = ?
                WHERE id = ?
                """,
                (
                    str(description or "")[:300],
                    json.dumps(list(tags), ensure_ascii=False),
                    json.dumps(list(keywords)[:8], ensure_ascii=False),
                    max(1, min(5, int(intensity))),
                    usage,
                    1 if safe else 0,
                    status,
                    str(sticker_id),
                ),
            )
            await self.conn.commit()

    async def set_tags(
        self,
        sticker_id: str,
        tags: Iterable[str],
        *,
        description: str | None = None,
        keywords: Iterable[str] | None = None,
        status: str = STATUS_READY,
    ) -> None:
        fields = ["tags = ?", "status = ?"]
        values: list[object] = [json.dumps(list(tags), ensure_ascii=False), status]
        if description is not None:
            fields.append("description = ?")
            values.append(str(description)[:300])
        if keywords is not None:
            fields.append("keywords = ?")
            values.append(json.dumps(list(keywords)[:8], ensure_ascii=False))
        values.append(str(sticker_id))
        async with self._lock:
            await self.conn.execute(
                f"UPDATE stickers SET {', '.join(fields)} WHERE id = ?", values
            )
            await self.conn.commit()

    async def set_status(self, sticker_id: str, status: str) -> None:
        if status not in (STATUS_READY, STATUS_PENDING, STATUS_DISABLED):
            return
        async with self._lock:
            await self.conn.execute(
                "UPDATE stickers SET status = ? WHERE id = ?",
                (status, str(sticker_id)),
            )
            await self.conn.commit()

    async def set_confirmed(self, sticker_id: str, confirmed: bool = True) -> bool:
        """人工确认后移入正常库；pending 且有标签的顺带转 ready。"""
        sticker = await self.get(sticker_id)
        if not sticker:
            return False
        status = sticker.status
        if confirmed and status == STATUS_PENDING and sticker.tags:
            status = STATUS_READY
        async with self._lock:
            await self.conn.execute(
                "UPDATE stickers SET confirmed = ?, status = ? WHERE id = ?",
                (1 if confirmed else 0, status, str(sticker_id)),
            )
            await self.conn.commit()
        return True

    async def set_description(self, sticker_id: str, description: str) -> bool:
        """只改描述，不改变 safe/usage；pending 且有标签的会自动转 ready。"""
        sticker = await self.get(sticker_id)
        if not sticker:
            return False
        status = (
            STATUS_READY
            if sticker.status == STATUS_PENDING and sticker.tags
            else sticker.status
        )
        async with self._lock:
            await self.conn.execute(
                "UPDATE stickers SET description = ?, status = ? WHERE id = ?",
                (str(description or "")[:300], status, str(sticker_id)),
            )
            await self.conn.commit()
        return True

    async def set_keywords(self, sticker_id: str, keywords: Iterable[str]) -> bool:
        """只改匹配关键词，不改变 safe/usage；pending 且有标签的会自动转 ready。"""
        sticker = await self.get(sticker_id)
        if not sticker:
            return False
        status = (
            STATUS_READY
            if sticker.status == STATUS_PENDING and sticker.tags
            else sticker.status
        )
        values = [str(k).strip()[:24] for k in keywords if str(k).strip()][:8]
        async with self._lock:
            await self.conn.execute(
                "UPDATE stickers SET keywords = ?, status = ? WHERE id = ?",
                (
                    json.dumps(values, ensure_ascii=False),
                    status,
                    str(sticker_id),
                ),
            )
            await self.conn.commit()
        return True

    async def update_fields(
        self,
        sticker_id: str,
        *,
        description: str | None = None,
        tags: Iterable[str] | None = None,
        keywords: Iterable[str] | None = None,
        usage: str | None = None,
        intensity: int | None = None,
        safe: bool | None = None,
        status: str | None = None,
        confirmed: bool | None = None,
    ) -> Sticker | None:
        """Web UI 用的一次性更新；未提供的字段保持不变。"""
        sticker = await self.get(sticker_id)
        if not sticker:
            return None

        fields: list[str] = []
        values: list[object] = []
        if description is not None:
            fields.append("description = ?")
            values.append(str(description)[:300])
        if tags is not None:
            fields.append("tags = ?")
            values.append(json.dumps(list(tags), ensure_ascii=False))
        if keywords is not None:
            fields.append("keywords = ?")
            values.append(
                json.dumps(
                    [str(k).strip()[:24] for k in keywords if str(k).strip()][:8],
                    ensure_ascii=False,
                )
            )
        if usage is not None:
            fields.append("usage = ?")
            values.append(usage if usage in ("reaction", "augment", "both") else "both")
        if intensity is not None:
            fields.append("intensity = ?")
            values.append(max(1, min(5, int(intensity))))
        if safe is not None:
            fields.append("safe = ?")
            values.append(1 if safe else 0)
        if confirmed is not None:
            fields.append("confirmed = ?")
            values.append(1 if confirmed else 0)

        if status is None:
            if sticker.status == STATUS_PENDING and (tags or sticker.tags):
                status = STATUS_READY
            else:
                status = sticker.status
        if status not in (STATUS_READY, STATUS_PENDING, STATUS_DISABLED):
            status = sticker.status
        fields.append("status = ?")
        values.append(status)

        values.append(str(sticker_id))
        async with self._lock:
            await self.conn.execute(
                f"UPDATE stickers SET {', '.join(fields)} WHERE id = ?", values
            )
            await self.conn.commit()
        return await self.get(sticker_id)

    async def set_usage(self, sticker_id: str, usage: str) -> None:
        if usage not in ("reaction", "augment", "both"):
            return
        async with self._lock:
            await self.conn.execute(
                "UPDATE stickers SET usage = ? WHERE id = ?",
                (usage, str(sticker_id)),
            )
            await self.conn.commit()

    async def set_safe(self, sticker_id: str, safe: bool) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE stickers SET safe = ? WHERE id = ?",
                (1 if safe else 0, str(sticker_id)),
            )
            await self.conn.commit()

    async def mark_used(self, sticker_ids: Iterable[str]) -> None:
        ids = [str(i) for i in sticker_ids if i]
        if not ids:
            return
        now = time.time()
        async with self._lock:
            for sticker_id in ids:
                await self.conn.execute(
                    "UPDATE stickers SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                    (now, sticker_id),
                )
            await self.conn.commit()

    async def delete(self, sticker_id: str) -> bool:
        sticker = await self.get(sticker_id)
        if not sticker:
            return False
        path = Path(sticker.path).resolve()
        base = self.stickers_dir.resolve()
        if path.parent == base:
            try:
                await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
            except OSError:
                pass
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM stickers WHERE id = ?", (str(sticker_id),)
            )
            await self.conn.commit()
        return True

    async def stats(self) -> dict:
        stickers = await self.list_all()
        tag_counts: dict[str, int] = {}
        status_counts = {"ready": 0, "pending": 0, "disabled": 0}
        for s in stickers:
            status_counts[s.status] = status_counts.get(s.status, 0) + 1
            for tag in s.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return {
            "total": len(stickers),
            "confirmed": sum(1 for s in stickers if s.confirmed),
            "new": sum(1 for s in stickers if not s.confirmed),
            "status": status_counts,
            "tags": dict(
                sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)
            ),
        }

"""角色注册表：把角色卡写入文件系统与 SQLite。"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.logging_setup import get_logger
from common.paths import app_root, cards_dir, relpath
from common.utils import iso_now

from ..database import Database
from ..database import crud
from .loader import CharacterCard, CharacterCardError, load_card, load_card_bytes

log = get_logger("bot.character_manager")


class CharacterRegistry:
    """角色池的读写入口。"""

    def __init__(self, db: Database, storage_dir: Optional[Path] = None):
        self.db = db
        self.storage_dir = Path(storage_dir) if storage_dir else cards_dir().parent
        self.avatars_dir = self.storage_dir / "avatars"
        self.cards_dir = self.storage_dir / "cards"
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        for path in (self.storage_dir, self.avatars_dir, self.cards_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 路径工具
    @staticmethod
    def abs_path(value: str) -> Optional[Path]:
        if not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = app_root() / candidate
        return candidate

    def avatar_file(self, row: Dict[str, Any]) -> Optional[Path]:
        path = self.abs_path(str(row.get("avatar_path") or ""))
        if path and path.exists():
            return path
        return None

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    # ---------------------------------------------------------------- 导入
    def _persist_assets(self, card: CharacterCard, character_id: str) -> Dict[str, str]:
        """保存头像与原始角色卡文件，返回相对路径。"""
        avatar_rel = ""
        if card.avatar_bytes:
            suffix = card.avatar_suffix if card.avatar_suffix.startswith(".") else ".png"
            avatar_file = self.avatars_dir / ("%s%s" % (character_id, suffix))
            try:
                avatar_file.write_bytes(card.avatar_bytes)
                avatar_rel = relpath(avatar_file)
            except Exception:
                avatar_rel = ""

        source_rel = ""
        if card.source_path:
            source = Path(card.source_path)
            if source.exists():
                target = self.cards_dir / ("%s%s" % (character_id, source.suffix or ".json"))
                try:
                    shutil.copy2(source, target)
                    source_rel = relpath(target)
                except Exception:
                    source_rel = ""
        return {"avatar_path": avatar_rel, "source_path": source_rel}

    async def _register(self, card: CharacterCard, overwrite: bool) -> Dict[str, Any]:
        existing = await crud.get_character_by_name(self.db, card.name)
        fields = card.to_db_fields()
        if existing and overwrite:
            character_id = str(existing["id"])
            assets = self._persist_assets(card, character_id)
            fields.update({k: v for k, v in assets.items() if v})
            row = await crud.update_character(self.db, character_id, fields)
            return {"status": "updated", "character": row, "card": card}

        character_id = self._new_id()
        fields["id"] = character_id
        assets = self._persist_assets(card, character_id)
        fields.update(assets)
        fields["enabled"] = 1
        fields["created_at"] = iso_now()
        row = await crud.insert_character(self.db, fields)
        return {"status": "created", "character": row, "card": card}

    async def import_file(self, path: Path, overwrite: bool = True) -> Dict[str, Any]:
        card = load_card(Path(path))
        return await self._register(card, overwrite)

    async def import_bytes(
        self, filename: str, data: bytes, overwrite: bool = True
    ) -> Dict[str, Any]:
        card = load_card_bytes(filename, data)
        return await self._register(card, overwrite)

    async def import_directory(self, directory: Path) -> Dict[str, Any]:
        directory = Path(directory)
        results: List[Dict[str, Any]] = []
        failures: List[Dict[str, str]] = []
        if not directory.exists():
            return {"imported": results, "failed": failures}
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in (".png", ".json", ".yaml", ".yml"):
                continue
            try:
                outcome = await self.import_file(path)
                results.append(
                    {
                        "file": path.name,
                        "status": outcome["status"],
                        "character": outcome["character"],
                    }
                )
            except CharacterCardError as exc:
                failures.append({"file": path.name, "error": str(exc)})
            except Exception as exc:  # pragma: no cover
                failures.append({"file": path.name, "error": "解析异常: %s" % exc})
        return {"imported": results, "failed": failures}

    # ------------------------------------------------------------ 自定义角色
    async def update_avatar(self, character_id: str, data: bytes, suffix: str = ".png") -> Optional[Dict[str, Any]]:
        """替换角色头像（用户在界面上自选图片）。"""
        row = await crud.get_character(self.db, character_id)
        if not row:
            return None
        if not data:
            raise CharacterCardError("头像文件为空")
        if len(data) > 8 * 1024 * 1024:
            raise CharacterCardError("头像图片过大（>8MB），请换一张小一点的")
        suffix = (suffix or ".png").lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            suffix = ".png"
        avatar_file = self.avatars_dir / ("%s%s" % (character_id, suffix))
        avatar_file.write_bytes(data)
        # 旧头像（不同扩展名）清掉，避免堆积
        old = self.avatar_file(row)
        if old and old != avatar_file and old.exists():
            try:
                old.unlink()
            except Exception:
                pass
        log.info("角色 [%s] 更换头像：%s", character_id, avatar_file.name)
        return await self.update(character_id, {"avatar_path": relpath(avatar_file)})

    async def create(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """不依赖角色卡，直接用界面填写的内容创建角色。"""
        name = str(fields.get("name") or "").strip()
        if not name:
            raise CharacterCardError("角色名称不能为空")
        existing = await crud.get_character_by_name(self.db, name)
        if existing:
            raise CharacterCardError("已存在同名角色「%s」，请换个名字或直接编辑它" % name)

        character_id = self._new_id()
        row = {
            "id": character_id,
            "name": name[:60],
            "description": str(fields.get("description") or ""),
            "personality": str(fields.get("personality") or ""),
            "scenario": str(fields.get("scenario") or ""),
            "first_mes": str(fields.get("first_mes") or ""),
            "mes_example": str(fields.get("mes_example") or ""),
            "system_prompt": str(fields.get("system_prompt") or ""),
            "creator_notes": str(fields.get("creator_notes") or ""),
            "tags": fields.get("tags") or [],
            "card_spec": "custom",
            "enabled": 1,
            "created_at": iso_now(),
        }
        created = await crud.insert_character(self.db, row)
        log.info("用户自定义创建角色：%s（%s）", row["name"], character_id)
        return await self.get(str(created.get("id") or character_id)) or created

    async def import_builtin(self, overwrite: bool = False) -> Dict[str, Any]:
        """导入随程序分发的内置角色卡。"""
        from common.paths import builtin_characters_dir

        directory = builtin_characters_dir()
        outcome = await self.import_directory(directory)
        outcome["directory"] = str(directory)
        outcome["builtin"] = True
        return outcome

    async def ensure_builtin(self, minimum: int = 1) -> Optional[Dict[str, Any]]:
        """角色池为空时自动导入内置角色（首次运行开箱可用）。"""
        try:
            count = await crud.count_characters(self.db)
        except Exception:  # pragma: no cover
            return None
        if count >= max(1, minimum):
            return None
        outcome = await self.import_builtin()
        imported = outcome.get("imported") or []
        if imported:
            log.info("角色池为空，已自动导入 %d 个内置角色", len(imported))
            return outcome
        return None

    # ---------------------------------------------------------------- 查询
    async def list_characters(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        rows = await crud.list_characters(self.db, enabled_only=enabled_only)
        counts = await crud.proactive_counts_today_by_character(self.db)
        for row in rows:
            row["enabled"] = int(row.get("enabled") or 0)
            row["tags_list"] = crud.tags_to_list(row.get("tags"))
            row["today_proactive"] = counts.get(str(row["id"]), 0)
            avatar = self.avatar_file(row)
            row["avatar_abs"] = str(avatar) if avatar else ""
        return rows

    async def get(self, character_id: str) -> Optional[Dict[str, Any]]:
        row = await crud.get_character(self.db, character_id)
        if row:
            row["enabled"] = int(row.get("enabled") or 0)
            row["tags_list"] = crud.tags_to_list(row.get("tags"))
            avatar = self.avatar_file(row)
            row["avatar_abs"] = str(avatar) if avatar else ""
        return row

    async def update(self, character_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        row = await crud.update_character(self.db, character_id, fields)
        return await self.get(str(row["id"])) if row else None

    async def set_enabled(self, character_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        await crud.set_character_enabled(self.db, character_id, enabled)
        return await self.get(character_id)

    async def delete(self, character_id: str) -> bool:
        row = await crud.get_character(self.db, character_id)
        if not row:
            return False
        for key in ("avatar_path", "source_path"):
            path = self.abs_path(str(row.get(key) or ""))
            if path and path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass
        return await crud.delete_character(self.db, character_id)

    async def enabled(self) -> List[Dict[str, Any]]:
        return await crud.list_characters(self.db, enabled_only=True)

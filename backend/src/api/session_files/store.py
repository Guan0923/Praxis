"""Session-scoped file upload, search, preview, and deletion.

The store owns the durable upload directory below a session's workspace
(``runtime/<session>/workspace/uploads``), searches the complete workspace,
and optionally searches the bound project directory.
All handlers here are workspace-confined and reject traversal, symbolic links
and special files.  Upload batches are staged to temporary files and renamed
atomically so a failed batch never leaves partial files behind.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from send2trash import send2trash

from backend.configuration import ClientPaths, ConfigurationError
from backend.domain.file_paths import FILE_SOURCES, ScopedPaths

MAX_FILES_PER_BATCH = 20
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 200 * 1024 * 1024
MAX_SEARCH_RESULTS = 100
MAX_WALKED_FILES = 20_000
MAX_EDITABLE_FILE_BYTES = 5_000_000
_CHUNK_SIZE = 1024 * 1024
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mini_agent",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"})
_WINDOWS_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOTS_SPACES = re.compile(r"[. ]+$")


class SessionFileError(ValueError):
    """A session file operation was rejected or failed."""


class SessionFileConflict(SessionFileError):
    """A file changed after the browser loaded it."""


def _mime_for(name: str) -> str:
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def is_image(name: str) -> bool:
    return Path(name).suffix.casefold() in _IMAGE_EXTENSIONS or _mime_for(name).startswith("image/")


class SessionFileStore:
    """Upload/search/preview/delete operations for one session."""

    def __init__(self, paths: ClientPaths, session_id: str, project_root: Path | None = None) -> None:
        self._paths = paths
        self._session_id = session_id
        self._project_root = Path(project_root).absolute() if project_root is not None else None
        self.file_paths = ScopedPaths(paths.session_workspace(session_id), self._project_root)

    @property
    def session_id(self) -> str:
        return self._session_id

    def upload_root(self) -> Path:
        """Return the canonical upload directory, creating it if needed."""

        try:
            self._paths.ensure_session(self._session_id)
        except ConfigurationError as exc:
            raise SessionFileError("上传目录不可用。") from exc
        return self._paths.session_uploads(self._session_id)

    def project_root(self) -> Path | None:
        """Return the searchable project root, or ``None`` when unavailable."""

        return self._project_root

    @staticmethod
    def sanitize_name(name: str) -> str:
        """Reduce a client filename to a safe display name.

        Path separators, control characters, Windows-reserved characters and
        trailing dots/spaces are stripped; a fully sanitized name falls back
        to ``file``.  The cleaned name is never used as a filesystem path on
        its own — :meth:`unique_target` resolves it against the upload root.
        """

        if not isinstance(name, str):
            name = str(name or "")
        normalized = unicodedata.normalize("NFKC", name)
        cleaned = _WINDOWS_RESERVED.sub("_", normalized).strip()
        cleaned = _TRAILING_DOTS_SPACES.sub("", cleaned)
        cleaned = cleaned.strip(" .")
        if not cleaned or cleaned in {".", ".."}:
            return "file"
        return cleaned[:200]

    @staticmethod
    def unique_target(upload_root: Path, name: str) -> tuple[Path, str]:
        """Return ``(path, stored_name)`` avoiding collisions with ``name (2).ext``."""

        base = Path(name)
        stem = base.stem or "file"
        suffix = base.suffix
        candidate = upload_root / name
        counter = 2
        while candidate.exists() or candidate.is_symlink():
            candidate = upload_root / f"{stem} ({counter}){suffix}"
            counter += 1
        return candidate, candidate.name

    def metadata(self, path: Path, source: str) -> dict[str, object]:
        resolved = self.resolve(source, str(path.absolute()))
        scoped = self.file_paths.format(resolved, scope="project" if source == "project" else "workspace")
        stat = path.stat()
        return {
            "source": source,
            "path": scoped,
            "display_path": scoped,
            "name": path.name,
            "size": stat.st_size,
            "mime": _mime_for(path.name),
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "is_image": is_image(path.name),
        }

    def _root_for(self, source: str) -> Path:
        if source == "workspace":
            return self.file_paths.workspace
        if source == "upload":
            root = self.upload_root()
            if root.is_symlink():
                raise SessionFileError("上传目录不能是符号链接。")
            return root.resolve()
        if source == "project":
            root = self.project_root()
            if root is None:
                raise SessionFileError("当前会话没有项目目录。")
            return root
        raise SessionFileError(f"不支持的引用来源：{source}")

    def _managed_root(self, source: str) -> Path:
        if source not in {"workspace", "project"}:
            raise SessionFileError("文件管理只支持 workspace 和 project。")
        return self._root_for(source).absolute()

    def _scoped(self, path: Path, source: str) -> str:
        root = self._managed_root(source)
        absolute = Path(os.path.abspath(path))
        if not absolute.is_relative_to(root):
            raise SessionFileError("文件路径超出允许范围。")
        relative = absolute.relative_to(root).as_posix()
        return f"{source}:{'' if relative == '.' else relative}"

    def _resolve_entry(self, source: str, path: str, *, allow_root: bool = False) -> Path:
        root = self._managed_root(source)
        expected_prefix = source
        prefix = path.partition(":")[0]
        if prefix in {"workspace", "project"} and prefix != expected_prefix:
            raise SessionFileError("文件路径前缀与来源不一致。")
        try:
            resolved = self.file_paths.resolve(path, allow_root=allow_root)
        except ValueError as exc:
            raise SessionFileError(str(exc)) from exc
        if resolved != root.resolve() and root.resolve() not in resolved.parents:
            raise SessionFileError("文件路径超出允许范围。")
        if not resolved.exists():
            raise SessionFileError("文件或目录不存在。")
        return resolved

    @staticmethod
    def _version(path: Path) -> str:
        info = path.stat()
        return f"{info.st_mtime_ns}:{info.st_size}"

    @staticmethod
    def _validate_entry_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise SessionFileError("名称不能为空。")
        value = name.strip()
        if value in {".", ".."} or _WINDOWS_RESERVED.search(value) or _TRAILING_DOTS_SPACES.search(value):
            raise SessionFileError("名称包含无效字符。")
        if len(value) > 200:
            raise SessionFileError("名称不能超过 200 个字符。")
        return value

    def roots(self) -> list[dict[str, object]]:
        return [
            {"source": "workspace", "path": "workspace:", "name": "workspace", "available": True},
            {
                "source": "project",
                "path": "project:",
                "name": "project",
                "available": self._project_root is not None,
            },
        ]

    def list_directory(self, source: str, path: str) -> list[dict[str, object]]:
        directory = self._resolve_entry(source, path, allow_root=True)
        if not directory.is_dir():
            raise SessionFileError("目标不是目录。")
        items: list[dict[str, object]] = []
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise SessionFileError("目录读取失败。") from exc
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            linked = entry.is_symlink() or bool(int(getattr(info, "st_file_attributes", 0)) & 0x400)
            if linked:
                kind = "link"
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "special"
            item: dict[str, object] = {
                "source": source,
                "path": self._scoped(entry_path, source),
                "name": entry.name,
                "kind": kind,
                "size": info.st_size if kind == "file" else None,
                "mtime": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
                "mime": _mime_for(entry.name) if kind == "file" else None,
                "is_image": kind == "file" and is_image(entry.name),
                "version": f"{info.st_mtime_ns}:{info.st_size}" if kind == "file" else None,
            }
            items.append(item)
        return sorted(items, key=lambda item: (item["kind"] != "directory", str(item["name"]).casefold()))

    @staticmethod
    def _looks_binary(raw: bytes) -> bool:
        if raw.startswith((b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x7fELF", b"MZ")):
            return True
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return False
        sample = raw[:8192]
        if b"\x00" in sample:
            return True
        controls = sum(byte < 32 and byte not in {8, 9, 10, 12, 13} for byte in sample)
        return bool(sample) and controls / len(sample) > 0.08

    @staticmethod
    def _decode_text(raw: bytes, requested: str | None = None) -> tuple[str, str, bool]:
        aliases = {"gbk": "gb18030", "utf-16": "utf-16-le"}
        encoding = aliases.get((requested or "").casefold(), (requested or "").casefold()) or None
        supported = {"utf-8", "utf-16-le", "utf-16-be", "gb18030"}
        if encoding is not None and encoding not in supported:
            raise SessionFileError("不支持所选文字编码。")
        bom_encoding: str | None = None
        bom_size = 0
        if raw.startswith(b"\xef\xbb\xbf"):
            bom_encoding, bom_size = "utf-8", 3
        elif raw.startswith(b"\xff\xfe"):
            bom_encoding, bom_size = "utf-16-le", 2
        elif raw.startswith(b"\xfe\xff"):
            bom_encoding, bom_size = "utf-16-be", 2
        candidates = [encoding] if encoding else ([bom_encoding] if bom_encoding else ["utf-8", "gb18030"])
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                offset = bom_size if bom_encoding == candidate else 0
                return raw[offset:].decode(candidate, errors="strict"), candidate, offset > 0
            except UnicodeDecodeError:
                continue
        raise SessionFileError("无法确定文字编码，请选择 UTF-8、UTF-16 或 GB18030。")

    def read_editor_file(self, source: str, path: str, encoding: str | None = None) -> dict[str, object]:
        resolved = self._resolve_entry(source, path)
        if not resolved.is_file():
            raise SessionFileError("目标不是文件。")
        info = resolved.stat()
        base = {
            "source": source,
            "path": self._scoped(resolved, source),
            "name": resolved.name,
            "size": info.st_size,
            "mime": _mime_for(resolved.name),
            "mtime": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
            "version": self._version(resolved),
        }
        if info.st_size > MAX_EDITABLE_FILE_BYTES:
            return {**base, "kind": "too_large", "limit": MAX_EDITABLE_FILE_BYTES}
        if is_image(resolved.name):
            return {**base, "kind": "image"}
        raw = resolved.read_bytes()
        if self._looks_binary(raw):
            return {**base, "kind": "binary"}
        try:
            content, detected, bom = self._decode_text(raw, encoding)
        except SessionFileError:
            if encoding is not None:
                raise
            return {
                **base,
                "kind": "encoding_required",
                "encodings": ["utf-8", "utf-16-le", "utf-16-be", "gb18030"],
            }
        newline = "\r\n" if "\r\n" in content else ("\r" if "\r" in content and "\n" not in content else "\n")
        return {
            **base,
            "kind": "text",
            "content": content,
            "encoding": detected,
            "bom": bom,
            "newline": newline,
        }

    def write_editor_file(
        self,
        source: str,
        path: str,
        *,
        content: str,
        encoding: str,
        bom: bool,
        newline: str,
        expected_version: str,
        force: bool = False,
    ) -> dict[str, object]:
        resolved = self._resolve_entry(source, path)
        if not resolved.is_file():
            raise SessionFileError("目标不是文件。")
        if not force and self._version(resolved) != expected_version:
            raise SessionFileConflict("文件已被其他程序修改。")
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        selected_newline = newline if newline in {"\n", "\r\n", "\r"} else "\n"
        normalized = normalized.replace("\n", selected_newline)
        canonical = {"gbk": "gb18030", "utf-16": "utf-16-le"}.get(encoding.casefold(), encoding.casefold())
        if canonical not in {"utf-8", "utf-16-le", "utf-16-be", "gb18030"}:
            raise SessionFileError("不支持所选文字编码。")
        try:
            raw = normalized.encode(canonical, errors="strict")
        except UnicodeEncodeError as exc:
            raise SessionFileError("当前文字编码无法保存这些字符。") from exc
        prefixes = {"utf-8": b"\xef\xbb\xbf", "utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}
        if bom and canonical in prefixes:
            raw = prefixes[canonical] + raw
        if len(raw) > MAX_EDITABLE_FILE_BYTES:
            raise SessionFileError("文件保存后超过 5 MB，已取消保存。")
        temporary = resolved.with_name(f".{resolved.name}.mini-agent-{uuid4().hex}.tmp")
        mode = stat.S_IMODE(resolved.stat().st_mode)
        try:
            with temporary.open("wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, resolved)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise SessionFileError("文件保存失败。") from exc
        return self.read_editor_file(source, self._scoped(resolved, source), canonical)

    def create_entry(self, source: str, parent_path: str, name: str, kind: str) -> dict[str, object]:
        parent = self._resolve_entry(source, parent_path, allow_root=True)
        if not parent.is_dir():
            raise SessionFileError("目标父目录无效。")
        target = parent / self._validate_entry_name(name)
        if target.exists() or target.is_symlink():
            raise SessionFileError("同名文件或目录已存在。")
        try:
            if kind == "file":
                target.write_text("", encoding="utf-8")
            elif kind == "directory":
                target.mkdir()
            else:
                raise SessionFileError("只能新建文件或目录。")
        except OSError as exc:
            raise SessionFileError("新建失败。") from exc
        return {"source": source, "path": self._scoped(target, source), "name": target.name, "kind": kind}

    @staticmethod
    def _numbered_target(parent: Path, name: str) -> Path:
        candidate = parent / name
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        base = Path(name)
        stem = base.stem if base.suffix else name
        suffix = base.suffix
        number = 1
        while True:
            candidate = parent / f"{stem}({number}){suffix}"
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
            number += 1

    def rename_entry(self, source: str, path: str, name: str) -> dict[str, object]:
        current = self._resolve_entry(source, path)
        target = current.with_name(self._validate_entry_name(name))
        if target.exists() or target.is_symlink():
            raise SessionFileError("同名文件或目录已存在。")
        try:
            current.rename(target)
        except OSError as exc:
            raise SessionFileError("重命名失败。") from exc
        return {"source": source, "path": self._scoped(target, source), "name": target.name}

    @staticmethod
    def _validate_tree_for_copy(source: Path) -> None:
        for directory, directories, files in os.walk(source, followlinks=False):
            base = Path(directory)
            for name in [*directories, *files]:
                item = base / name
                info = item.lstat()
                if item.is_symlink() or int(getattr(info, "st_file_attributes", 0)) & 0x400:
                    raise SessionFileError("包含链接的目录不能移动。")
                if not item.is_file() and not item.is_dir():
                    raise SessionFileError("包含特殊文件的目录不能移动。")

    def move_entry(self, source: str, path: str, target_source: str, target_parent_path: str) -> dict[str, object]:
        current = self._resolve_entry(source, path)
        parent = self._resolve_entry(target_source, target_parent_path, allow_root=True)
        if not parent.is_dir():
            raise SessionFileError("移动目标不是目录。")
        if current.is_dir() and (parent == current or parent.is_relative_to(current)):
            raise SessionFileError("目录不能移动到自身内部。")
        target = self._numbered_target(parent, current.name)
        temporary: Path | None = None
        try:
            if source == target_source:
                current.rename(target)
            else:
                temporary = target.with_name(f".{target.name}.mini-agent-{uuid4().hex}.tmp")
                if current.is_dir():
                    self._validate_tree_for_copy(current)
                    shutil.copytree(current, temporary)
                else:
                    shutil.copy2(current, temporary)
                os.replace(temporary, target)
                try:
                    shutil.rmtree(current) if current.is_dir() else current.unlink()
                except OSError:
                    shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
                    raise
        except (OSError, shutil.Error) as exc:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary) if temporary.is_dir() else temporary.unlink(missing_ok=True)
            raise SessionFileError("移动失败。") from exc
        return {
            "source": target_source,
            "path": self._scoped(target, target_source),
            "name": target.name,
        }

    def recycle_entry(self, source: str, path: str) -> None:
        target = self._resolve_entry(source, path)
        try:
            send2trash(str(target))
        except Exception as exc:
            raise SessionFileError("删除失败。") from exc

    def store_batch(self, items: Sequence[tuple[str, object]]) -> list[dict[str, object]]:
        """Stream a multipart batch into the upload directory atomically.

        ``items`` is a sequence of ``(client_filename, readable)`` pairs where
        ``readable`` exposes a synchronous ``read(size)``.  Every file is
        staged to a temporary sibling first; only after the whole batch
        validates are the temporary files renamed to their final names.  A
        failure anywhere removes all staged and already-renamed files, so a
        rejected batch never leaves partial uploads behind.
        """

        if len(items) > MAX_FILES_PER_BATCH:
            raise SessionFileError(f"单次最多上传 {MAX_FILES_PER_BATCH} 个文件。")
        upload_root = self.upload_root()
        staged: list[tuple[Path, str]] = []
        renamed: list[Path] = []
        current_temporary: Path | None = None
        total = 0
        try:
            for index, (client_name, readable) in enumerate(items):
                filename = self.sanitize_name(client_name or f"file-{index + 1}")
                temporary = upload_root / f".upload-{uuid4().hex}.tmp"
                current_temporary = temporary
                staged.append((temporary, filename))
                size = 0
                with temporary.open("wb") as handle:
                    while True:
                        chunk = readable.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise SessionFileError(f"单文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 限制：{filename}")
                        if total > MAX_BATCH_BYTES:
                            raise SessionFileError(f"单次上传合计超过 {MAX_BATCH_BYTES // (1024 * 1024)} MiB 限制。")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                current_temporary = None
            for temporary, filename in staged:
                target, stored_name = self.unique_target(upload_root, filename)
                os.replace(temporary, target)
                renamed.append(target)
            return [self.metadata(path, "upload") for path in renamed]
        except Exception:
            if current_temporary is not None:
                current_temporary.unlink(missing_ok=True)
            for temporary, _filename in staged:
                temporary.unlink(missing_ok=True)
            for path in renamed:
                path.unlink(missing_ok=True)
            raise

    def search(self, q: str, limit: int = 20) -> list[dict[str, object]]:
        """Search both complete roots, retaining upload ownership labels."""

        if not isinstance(limit, int) or limit < 1:
            raise SessionFileError("limit 必须是正整数。")
        limit = min(limit, MAX_SEARCH_RESULTS)
        query = (q or "").strip().replace("\\", "/").casefold()
        scope, separator, relative_query = query.partition(":")
        search_scope = scope if separator and scope in {"workspace", "project"} else None
        if search_scope:
            query = relative_query
        results: list[dict[str, object]] = []
        upload_root = self.upload_root()
        roots: list[tuple[Path, str]] = []
        if self._project_root is not None and search_scope != "workspace":
            roots.append((self._project_root, "project"))
        if search_scope != "project":
            roots.append((self.file_paths.workspace, "workspace"))
        seen: set[Path] = set()
        for root, scope in roots:
            if len(results) >= limit:
                break
            for path in self._iter_files(root):
                if len(results) >= limit:
                    break
                relative = path.relative_to(root).as_posix()
                if query and query not in relative.casefold() and query not in path.name.casefold():
                    continue
                if path in seen:
                    continue
                seen.add(path)
                source = "upload" if path.is_relative_to(upload_root) else scope
                results.append(self.metadata(path, source))
        return results[:limit]

    def _iter_files(self, root: Path, skipped_dirs: set[str] | None = None) -> Iterable[Path]:
        """Yield regular files inside ``root`` using the tool ignore policy.

        The walk never follows symbolic links, ignores the standard tool
        ignore directories, and stops after a bounded number of examined
        files so a huge project cannot stall the search endpoint.
        """

        ignored = _IGNORED_DIRECTORIES | (skipped_dirs or set())
        try:
            ScopedPaths.reject_links(root, root)
        except ValueError as exc:
            raise SessionFileError(str(exc)) from exc
        examined = 0
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directories[:] = sorted(
                name for name in directories if name not in ignored and not self._is_link(directory_path / name)
            )
            for name in sorted(filenames):
                examined += 1
                if examined > MAX_WALKED_FILES:
                    return
                file_path = directory_path / name
                if self._is_link(file_path) or not file_path.is_file():
                    continue
                resolved = file_path.resolve()
                if resolved == root or root not in resolved.parents:
                    continue
                yield resolved

    @staticmethod
    def _is_link(path: Path) -> bool:
        try:
            ScopedPaths.reject_links(path, path)
        except ValueError:
            return True
        return False

    def resolve(self, source: str, path: str) -> Path:
        """Resolve a reference while checking both scope and source ownership."""

        root = self._root_for(source)
        try:
            prefix = path.partition(":")[0]
            expected_scope = "project" if source == "project" else "workspace"
            if prefix in {"workspace", "project"} and prefix != expected_scope:
                raise ValueError("文件路径前缀与来源不一致。")
            resolved = self.file_paths.resolve(path)
        except ValueError as exc:
            raise SessionFileError(str(exc)) from exc
        root = root.resolve()
        if resolved != root and root not in resolved.parents:
            raise SessionFileError("文件路径超出允许范围。")
        if not resolved.is_file():
            raise SessionFileError("引用的文件不存在。")
        return resolved

    def normalize_references(self, values: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
        """Validate browser references and return canonical scoped payloads."""

        references: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for value in values:
            source = value.get("source")
            path = value.get("path")
            if source not in FILE_SOURCES or not isinstance(path, str) or not path:
                raise SessionFileError("无效的文件引用。")
            resolved = self.resolve(str(source), path)
            canonical_source = (
                "upload" if resolved.is_relative_to(self._paths.session_uploads(self._session_id)) else str(source)
            )
            scoped = self.file_paths.format(resolved, scope="project" if canonical_source == "project" else "workspace")
            key = (canonical_source, scoped)
            if key in seen:
                continue
            seen.add(key)
            references.append(
                {
                    "source": key[0],
                    "path": key[1],
                    "display_path": scoped,
                }
            )
        return references

    def delete_upload(self, path: str) -> None:
        """Delete one uploaded file; project files are never deleted here."""

        resolved = self.resolve("upload", path)
        resolved.unlink()


def remove_upload_tree(upload_root: Path) -> None:
    """Remove a session's canonical upload directory without following links."""

    if upload_root.is_symlink():
        raise SessionFileError("上传目录不能是符号链接。")
    if upload_root.exists():
        if not upload_root.is_dir():
            raise SessionFileError("上传路径必须是目录。")
        for item in upload_root.rglob("*"):
            if item.is_symlink():
                raise SessionFileError(f"上传目录包含符号链接：{item.name}")
            if not item.is_file() and not item.is_dir():
                raise SessionFileError(f"上传目录包含特殊文件：{item.name}")
        shutil.rmtree(upload_root)


__all__ = [
    "MAX_BATCH_BYTES",
    "MAX_FILE_BYTES",
    "MAX_FILES_PER_BATCH",
    "MAX_EDITABLE_FILE_BYTES",
    "MAX_SEARCH_RESULTS",
    "SessionFileConflict",
    "SessionFileError",
    "SessionFileStore",
    "is_image",
    "remove_upload_tree",
]

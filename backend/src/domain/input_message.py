"""Immutable input shared by delivery, mailbox and runtime consumers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .file_paths import FILE_SOURCES, is_reference_path


@dataclass(frozen=True, slots=True)
class FileReference:
    path: str
    source: str | None = None
    display_path: str | None = None

    def __post_init__(self) -> None:
        if not self.path or not is_reference_path(self.path):
            raise ValueError("Invalid reference path.")
        if self.source is not None and (self.source not in FILE_SOURCES or not self.display_path):
            raise ValueError("Invalid reference source or display path.")
        if self.source is None and self.display_path is not None:
            raise ValueError("A display path requires a reference source.")

    @classmethod
    def from_mapping(cls, value: Mapping[str, str]) -> FileReference:
        return cls(path=value["path"], source=value.get("source"), display_path=value.get("display_path"))

    def to_dict(self) -> dict[str, str]:
        if self.source is None:
            return {"path": self.path}
        return {"source": self.source, "path": self.path, "display_path": self.display_path or ""}


@dataclass(frozen=True, slots=True)
class InputMessage:
    text: str
    references: tuple[FileReference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or (not self.text.strip() and not self.references):
            raise ValueError("Message requires text or file references.")
        if not isinstance(self.references, tuple) or any(
            not isinstance(item, FileReference) for item in self.references
        ):
            raise TypeError("Message references must be immutable FileReference values.")

    @classmethod
    def from_input(cls, text: str, references: Sequence[Mapping[str, str]] = ()) -> InputMessage:
        return cls(text, tuple(FileReference.from_mapping(item) for item in references))

    def reference_dicts(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.references]

    def to_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {"type": "text", "text": self.text, "status": "success"}
        if self.references:
            item["references"] = self.reference_dicts()
        return item

    def model_text(self) -> str:
        if not self.references:
            return self.text
        return (
            self.text
            + "\n\nFile references:\n"
            + "\n".join(
                f"- @{item.path.replace(chr(92), '/')}" + (f" ({item.source})" if item.source else "")
                for item in self.references
            )
        )

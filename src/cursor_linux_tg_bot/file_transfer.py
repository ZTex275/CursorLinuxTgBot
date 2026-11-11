"""Просмотр файлов на сервере и подготовка путей для загрузки/скачивания."""
from __future__ import annotations

import os
from pathlib import Path

from .config import FilesConfig

_UPLOAD_COMMANDS = {"upload", "put"}


class FileTransferError(ValueError):
    pass


def format_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(value)} Б"


def parse_transfer_command(text: str) -> tuple[str, str]:
    """Возвращает (команда, аргумент) для `/upload` в подписи к файлу."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return "", ""
    token, _, rest = stripped.partition(" ")
    command = token[1:].split("@", 1)[0].lower()
    return command, rest.strip()


def is_upload_command(text: str) -> bool:
    command, _ = parse_transfer_command(text)
    return command in _UPLOAD_COMMANDS


def sanitize_filename(name: str, fallback: str = "file.bin") -> str:
    cleaned = (name or "").replace("\x00", "").replace("\\", "/").split("/")[-1].strip()
    if cleaned in {"", ".", ".."}:
        return fallback
    return cleaned


def _workspace_path(workspace: str) -> Path:
    return Path(workspace).expanduser().resolve()


def _check_workspace(path: Path, workspace: str, restrict_to_workspace: bool) -> Path:
    if not restrict_to_workspace:
        return path
    base = _workspace_path(workspace)
    try:
        path.relative_to(base)
    except ValueError as err:
        raise FileTransferError(
            f"Путь вне workspace запрещён: {path}\n"
            "Измените bot.files.restrict_to_workspace в config.yaml, если это нужно."
        ) from err
    return path


def resolve_path(
    raw: str,
    workspace: str,
    *,
    restrict_to_workspace: bool = False,
) -> Path:
    value = (raw or "").strip().strip('"').strip("'")
    base = _workspace_path(workspace)
    if not value:
        return _check_workspace(base, workspace, restrict_to_workspace)

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate.absolute()
    return _check_workspace(resolved, workspace, restrict_to_workspace)


def resolve_upload_target(
    raw: str,
    filename: str,
    workspace: str,
    *,
    restrict_to_workspace: bool = False,
) -> Path:
    """Определяет итоговый файл.

    Аргумент считается каталогом, если он пустой, оканчивается на `/` или `\\`,
    уже существует как каталог, либо не имеет расширения. Если указан путь с
    расширением (например, `src/new_name.py`), он используется как имя файла.
    """
    safe_name = sanitize_filename(filename)
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        target_dir = _workspace_path(workspace)
        _check_workspace(target_dir, workspace, restrict_to_workspace)
        target = target_dir / safe_name
    else:
        path = resolve_path(value, workspace, restrict_to_workspace=restrict_to_workspace)
        if value.endswith(("/", "\\")) or path.is_dir():
            target = path / safe_name
        elif path.exists():
            target = path
        elif path.suffix:
            target = path
        else:
            target = path / safe_name

    if target.exists() and target.is_dir():
        target = target / safe_name
    _check_workspace(target, workspace, restrict_to_workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def save_bytes(target: Path, data: bytes, *, overwrite: bool = True) -> None:
    if target.exists() and not overwrite:
        raise FileTransferError(f"Файл уже существует: {target}")
    tmp = target.with_name(target.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as err:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise FileTransferError(f"Не удалось сохранить файл: {err}") from err


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:8192]
    if b"\x00" in sample:
        return True
    controls = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return controls / len(sample) > 0.05


def list_directory(
    raw: str,
    workspace: str,
    files: FilesConfig,
) -> str:
    path = resolve_path(
        raw,
        workspace,
        restrict_to_workspace=files.restrict_to_workspace,
    )
    if not path.exists():
        raise FileTransferError(f"Путь не найден: {path}")
    if path.is_file():
        return f"📄 Это файл: {path}\nРазмер: {format_size(path.stat().st_size)}\nОткрыть: /cat {path}"

    try:
        entries = sorted(
            path.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.casefold()),
        )
    except OSError as err:
        raise FileTransferError(f"Не удалось прочитать каталог: {err}") from err

    lines = [f"📁 {path}"]
    shown = entries[: files.max_list_entries]
    if not shown:
        lines.append("\n(пусто)")
    else:
        lines.append("")
        for entry in shown:
            try:
                if entry.is_dir():
                    lines.append(f"📁 {entry.name}/")
                else:
                    size = entry.stat().st_size
                    lines.append(f"📄 {entry.name}  {format_size(size)}")
            except OSError:
                lines.append(f"❔ {entry.name} (нет доступа)")

    if len(entries) > len(shown):
        lines.append(
            f"\nПоказаны первые {len(shown)} из {len(entries)}. "
            "Уточните путь, чтобы увидеть остальное."
        )
    lines.append("\nОткрыть файл: /cat <путь>\nСкачать: /download <путь>")
    return "\n".join(lines)


def read_file_preview(
    raw: str,
    workspace: str,
    files: FilesConfig,
) -> str:
    path = resolve_path(
        raw,
        workspace,
        restrict_to_workspace=files.restrict_to_workspace,
    )
    if not path.exists():
        raise FileTransferError(f"Путь не найден: {path}")
    if path.is_dir():
        return list_directory(raw, workspace, files)

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(files.max_view_bytes + 1)
    except OSError as err:
        raise FileTransferError(f"Не удалось прочитать файл: {err}") from err

    if _looks_binary(data):
        raise FileTransferError(
            f"Похоже, это двоичный файл: {path} ({format_size(size)}).\n"
            f"Скачать: /download {path}"
        )

    truncated = len(data) > files.max_view_bytes
    if truncated:
        data = data[: files.max_view_bytes]
    text = data.decode("utf-8", errors="replace")
    header = f"📄 {path} ({format_size(size)}"
    header += ", показано начало" if truncated else ""
    header += ")"
    body = text if text.strip() else "(файл пуст)"
    result = f"{header}\n\n{body}"
    if truncated:
        result += (
            f"\n\n… показано {format_size(files.max_view_bytes)} из {format_size(size)}. "
            f"Скачать целиком: /download {path}"
        )
    return result


def validate_download(
    raw: str,
    workspace: str,
    files: FilesConfig,
) -> Path:
    path = resolve_path(
        raw,
        workspace,
        restrict_to_workspace=files.restrict_to_workspace,
    )
    if not path.exists():
        raise FileTransferError(f"Путь не найден: {path}")
    if path.is_dir():
        raise FileTransferError(f"Это каталог, а не файл: {path}\nПосмотреть: /ls {path}")
    if not path.is_file():
        raise FileTransferError(f"Это не обычный файл: {path}")

    size = path.stat().st_size
    limit = int(files.max_download_mb * 1024 * 1024)
    if size > limit:
        raise FileTransferError(
            f"Файл слишком большой: {format_size(size)} "
            f"(лимит {format_size(limit)})."
        )
    return path


def check_upload_size(size: int, files: FilesConfig) -> None:
    limit = int(files.max_upload_mb * 1024 * 1024)
    if size and size > limit:
        raise FileTransferError(
            f"Файл слишком большой: {format_size(size)} "
            f"(лимит {format_size(limit)})."
        )

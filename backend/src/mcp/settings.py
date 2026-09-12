"""Atomic user-facing MCP server and credential settings."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from urllib.parse import unquote_plus

from backend.configuration import ClientPaths, ConfigurationError, atomic_write_text

from .config import McpServerConfig, read_server_configs, sensitive_environment_name, sensitive_header_name
from .json_config import STORED_SECRET, McpJsonDocument


class McpServerNotFound(ValueError):
    """The requested configured server does not exist."""


KEYRING_SERVICE = "praxis-mcp"
_MANAGED_REFERENCE_PREFIX = f"keyring://{KEYRING_SERVICE}/"


def _account(server_name: str, environment_name: str) -> str:
    return f"{server_name}.{environment_name}"


def _reference(server_name: str, environment_name: str) -> str:
    return f"{_MANAGED_REFERENCE_PREFIX}{_account(server_name, environment_name)}"


def _managed_account(reference: str) -> str | None:
    return reference[len(_MANAGED_REFERENCE_PREFIX) :] if reference.startswith(_MANAGED_REFERENCE_PREFIX) else None


def _header_key(name: str) -> str:
    return "http." + name.lower().encode("ascii").hex()


def _references(server: McpServerConfig) -> dict[str, str]:
    references = {f"env.{key}": value for key, value in (server.env_refs or {}).items()}
    references.update({f"header.{key}": value for key, value in (server.header_refs or {}).items()})
    if server.url_ref:
        references["url"] = server.url_ref
    return references


def _masked_url(url: str) -> str:
    base, separator, query = url.partition("?")
    if not separator:
        return url
    parts: list[str] = []
    for part in query.split("&"):
        key, equals, value = part.partition("=")
        if equals and sensitive_header_name(unquote_plus(key)):
            part = key + "=" + STORED_SECRET
        parts.append(part)
    return base + "?" + "&".join(parts)


def _keyring_module():
    import keyring

    return keyring


def _render_servers(configs: tuple[McpServerConfig, ...]) -> str:
    lines: list[str] = []
    for server in sorted(configs, key=lambda item: item.name):
        lines.extend(
            (
                f"[servers.{server.name}]",
                f"transport = {json.dumps(server.transport)}",
                f"enabled = {'true' if server.enabled else 'false'}",
                f"timeout = {server.timeout}",
            )
        )
        if server.transport == "stdio":
            lines.append(f"command = {json.dumps(server.command, ensure_ascii=False)}")
            lines.append("args = [" + ", ".join(json.dumps(item, ensure_ascii=False) for item in server.args) + "]")
        else:
            lines.append(f"url = {json.dumps(server.url, ensure_ascii=False)}")
        if server.url_ref is not None:
            lines.append(f"url_ref = {json.dumps(server.url_ref)}")
        if server.cwd is not None:
            lines.append(f"cwd = {json.dumps(server.cwd, ensure_ascii=False)}")
        if server.env:
            lines.append("")
            lines.append(f"[servers.{server.name}.env]")
            lines.extend(
                f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in sorted(server.env.items())
            )
        if server.env_refs:
            lines.append("")
            lines.append(f"[servers.{server.name}.env_refs]")
            lines.extend(
                f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in sorted(server.env_refs.items())
            )
        for field, values in (("headers", server.headers), ("header_refs", server.header_refs)):
            if values:
                lines.extend(("", f"[servers.{server.name}.{field}]"))
                lines.extend(
                    f"{json.dumps(name)} = {json.dumps(value, ensure_ascii=False)}"
                    for name, value in sorted(values.items())
                )
        lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


class McpSettingsStore:
    """Own structured servers.toml mutations and managed keyring entries."""

    def __init__(self, paths: ClientPaths) -> None:
        self.paths = paths
        self.lock_path = paths.mcp_file.with_name(".servers.toml.lock")

    def servers(self) -> tuple[McpServerConfig, ...]:
        return read_server_configs(self.paths.mcp_file, reject_plaintext_secrets=True)

    def server(self, name: str) -> McpServerConfig:
        found = next((item for item in self.servers() if item.name == name), None)
        if found is None:
            raise McpServerNotFound("MCP server not found")
        return found

    def document(self) -> dict[str, object]:
        return {"mcpServers": {server.name: self.json_server(server) for server in self.servers()}}

    @staticmethod
    def json_server(server: McpServerConfig) -> dict[str, object]:
        result: dict[str, object] = {
            "type": "streamableHttp" if server.transport == "streamable_http" else server.transport,
            "timeout": server.timeout,
            "disabled": not server.enabled,
        }
        if server.transport == "stdio":
            result.update(command=server.command, args=list(server.args))
            result["env"] = {**(server.env or {}), **{key: STORED_SECRET for key in server.env_refs or {}}}
            if server.cwd is not None:
                result["cwd"] = server.cwd
        else:
            result["url"] = server.url
            result["headers"] = {**(server.headers or {}), **{key: STORED_SECRET for key in server.header_refs or {}}}
        return result

    def replace_document(self, document: McpJsonDocument) -> dict[str, object]:
        with self._lock():
            previous = {server.name: server for server in self.servers()}
            servers: list[McpServerConfig] = []
            sets: dict[str, str] = {}
            for name, values in document.mcpServers.items():
                old = previous.get(name)
                env, env_refs = self._split_secrets(name, values.env, old.env_refs if old else None, sets, header=False)
                headers, header_refs = self._split_secrets(
                    name, values.headers, old.header_refs if old else None, sets, header=True
                )
                url, url_ref = values.url, None
                if url is not None:
                    if STORED_SECRET in url:
                        if old is None or old.url != url or not old.url_ref:
                            raise ValueError("Replace stored URL placeholders with the full URL before changing it")
                        url_ref = old.url_ref
                    else:
                        public_url = _masked_url(url)
                        if public_url != url:
                            url_ref = _reference(name, "url")
                            sets[_account(name, "url")] = url
                            url = public_url
                servers.append(
                    McpServerConfig(
                        name=name,
                        command=values.command or "",
                        args=tuple(values.args),
                        cwd=values.cwd,
                        env=env or None,
                        env_refs=env_refs or None,
                        enabled=not values.disabled,
                        transport="streamable_http" if values.type == "streamableHttp" else values.type,
                        url=url,
                        headers=headers or None,
                        header_refs=header_refs or None,
                        timeout=values.timeout,
                        url_ref=url_ref,
                    )
                )
            retained = {ref for server in servers for ref in _references(server).values()}
            removed = tuple(
                account
                for server in previous.values()
                for ref in _references(server).values()
                if ref not in retained and (account := _managed_account(ref)) is not None
            )
            self._commit(tuple(servers), credential_sets=sets, credential_deletes=removed)
            return {"mcpServers": {server.name: self.json_server(server) for server in servers}}

    @staticmethod
    def _split_secrets(
        server: str,
        values: Mapping[str, str],
        previous: Mapping[str, str] | None,
        sets: dict[str, str],
        *,
        header: bool,
    ) -> tuple[dict[str, str], dict[str, str]]:
        plain: dict[str, str] = {}
        references: dict[str, str] = {}
        for key, value in values.items():
            if value == STORED_SECRET:
                if previous is None or key not in previous:
                    raise ValueError("Stored secret placeholder has no saved value; enter the actual value")
                references[key] = previous[key]
            elif sensitive_header_name(key) if header else sensitive_environment_name(key):
                account_key = _header_key(key) if header else key
                references[key] = _reference(server, account_key)
                sets[_account(server, account_key)] = value
            else:
                plain[key] = value
        return plain, references

    def _commit(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        credential_sets: Mapping[str, str] | None = None,
        credential_deletes: tuple[str, ...] = (),
    ) -> None:
        sets = dict(credential_sets or {})
        touched = tuple(dict.fromkeys((*sets.keys(), *credential_deletes)))
        if not touched:
            atomic_write_text(self.paths.mcp_file, _render_servers(configs))
            return
        keyring = _keyring_module()
        previous: dict[str, str | None] = {}
        try:
            for account in touched:
                previous[account] = keyring.get_password(KEYRING_SERVICE, account)
            for account, value in sets.items():
                keyring.set_password(KEYRING_SERVICE, account, value)
            for account in credential_deletes:
                if previous.get(account) is not None:
                    keyring.delete_password(KEYRING_SERVICE, account)
            atomic_write_text(self.paths.mcp_file, _render_servers(configs))
        except Exception:
            for account, value in previous.items():
                try:
                    if value is None:
                        if keyring.get_password(KEYRING_SERVICE, account) is not None:
                            keyring.delete_password(KEYRING_SERVICE, account)
                    else:
                        keyring.set_password(KEYRING_SERVICE, account, value)
                except Exception:
                    pass
            raise ValueError("Unable to save MCP credentials or configuration.") from None

    @contextmanager
    def _lock(self) -> Iterator[None]:
        deadline = time.monotonic() + 10
        handle = None
        while handle is None:
            try:
                handle = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ConfigurationError("Timed out waiting for MCP settings lock")
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(handle)
            self.lock_path.unlink(missing_ok=True)


__all__ = ["KEYRING_SERVICE", "McpSettingsStore"]

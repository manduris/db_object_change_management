"""Connection profiles stored by the application."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConnectionProfile:
    id: str
    name: str
    host: str = ""
    port: int = 1521
    identifier_type: str = "service_name"
    identifier: str = ""
    username: str = ""
    password: str = ""
    current_schema: str = ""
    dsn: str = ""
    password_error: bool = False

    def target_text(self) -> str:
        user = self.username or "-"
        custom = self.dsn.strip()
        if custom:
            shown = custom if len(custom) <= 48 else custom[:45] + "..."
            return f"{user} @ {shown}"
        ident = self.identifier or "-"
        return f"{user}@{self.host}:{self.port}/{ident}"

    def snapshot(self) -> ConnectionProfile:
        return ConnectionProfile(
            id=self.id,
            name=self.name,
            host=self.host,
            port=self.port,
            identifier_type=self.identifier_type,
            identifier=self.identifier,
            username=self.username,
            password=self.password,
            current_schema=self.current_schema,
            dsn=self.dsn,
            password_error=self.password_error,
        )

    def __repr__(self) -> str:
        return (
            f"ConnectionProfile(id={self.id!r}, name={self.name!r}, "
            f"username={self.username!r}, target={self.target_text()!r})"
        )


def connection_fingerprint(profile: ConnectionProfile) -> tuple:
    """Identity of the connection settings that require a new session."""
    return (
        profile.host.strip(),
        int(profile.port),
        profile.identifier_type,
        profile.identifier.strip(),
        profile.username,
        profile.password,
        profile.current_schema.strip(),
        profile.dsn.strip(),
    )

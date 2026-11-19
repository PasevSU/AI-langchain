"""Exception hierarchy for the installer agent.

Every error carries a stable ``code`` so that the CLI, the journal and the
release report can classify failures without parsing English messages.
"""

from __future__ import annotations


class InstallerError(RuntimeError):
    """Base class for all installer-agent failures."""

    code = "installer-error"


class ConfigError(InstallerError):
    """The installer configuration file is missing or malformed."""

    code = "config-invalid"


class RegistryError(InstallerError):
    """An agent declaration is missing, malformed or internally inconsistent."""

    code = "registry-invalid"


class StateError(InstallerError):
    """The installer state directory or state file is unusable."""

    code = "state-invalid"


class LockError(StateError):
    """Another installer process already holds the state lock."""

    code = "state-locked"


class BackupError(InstallerError):
    """The mandatory pre-migration backup release could not be created."""

    code = "backup-failed"


class OperationError(InstallerError):
    """A declared change cannot be applied to the repository."""

    code = "operation-failed"


class PublishError(InstallerError):
    """The update could not be published (commit, tag or push)."""

    code = "publish-failed"

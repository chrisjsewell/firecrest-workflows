"""Database objects for data handling.

Client
    |_ Code
       |_ CalcJob <-> Processing
            |_ DataNode

See also: https://docs.sqlalchemy.org/en/20/orm/quickstart.html
"""
from pathlib import PurePosixPath, PureWindowsPath
import random
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from uuid import uuid4

import firecrest
from sqlalchemy import JSON, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# TODO versioning


class Base(DeclarativeBase):
    """Base class for all tables."""

    __abstract__ = True

    pk: Mapped[int] = mapped_column(primary_key=True)
    """The primary key set by the database."""

    def __str__(self) -> str:
        """Return a string representation of the object."""
        return f"{self.__class__.__name__}({self.pk})"

    def __eq__(self, other: Any) -> bool:
        """Return True if the objects are equal."""
        if not isinstance(other, self.__class__):
            return False
        return self.pk == other.pk

    def __hash__(self) -> int:
        """Return the hash of the object."""
        return hash(self.pk)


class Client(Base):
    """Data for a single-user to interact with FirecREST."""

    __tablename__ = "client"

    label: Mapped[str] = mapped_column(
        unique=True, default=lambda: random.choice(NAMES)
    )
    client_url: Mapped[str]

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.pk}, {self.label})"

    # per-user authinfo
    client_id: Mapped[str]
    token_uri: Mapped[str]
    client_secret: Mapped[str]  # TODO this should not be stored in the database
    machine_name: Mapped[str]
    work_dir: Mapped[str]
    """The working directory for the user on the remote machine."""
    fsystem: Mapped[Literal["posix", "windows"]] = mapped_column(
        Enum("posix", "windows"), default="posix"
    )
    """The file system type on the remote machine."""
    small_file_size_mb: Mapped[int] = mapped_column(default=5)
    """The maximum size of a file that can be uploaded directly, in MB."""

    codes: Mapped[List["Code"]] = relationship("Code")
    """The codes that are associated with this client."""

    @property
    def work_path(self) -> Union[PurePosixPath, PureWindowsPath]:
        """Return the work directory path."""
        return (
            PurePosixPath(self.work_dir)
            if self.fsystem == "posix"
            else PureWindowsPath(self.work_dir)
        )

    @property
    def client(self) -> firecrest.Firecrest:
        """Return a FirecREST client.

        Cache the client instance, so that we don't have to re-authenticate
        (it automatically refreshes the token when it expires)
        """
        if not hasattr(self, "_client"):
            self._client = firecrest.Firecrest(
                firecrest_url=self.client_url,
                authorization=firecrest.ClientCredentialsAuth(
                    self.client_id, self.client_secret, self.token_uri
                ),
            )
        return self._client


class Code(Base):
    """Data for a single code."""

    __tablename__ = "code"
    __table_args__ = (UniqueConstraint("client_pk", "label"),)

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.pk}, {self.label})"

    label: Mapped[str] = mapped_column(default=lambda: random.choice(NAMES))

    client_pk: Mapped[int] = mapped_column(ForeignKey("client.pk"))
    """The primary key of the client that this code is associated with."""
    client: Mapped[Client] = relationship("Client", back_populates="codes")
    """The client that this code is associated with."""

    script: Mapped[str]
    """The batch script template to submit to the scheduler on the remote machine.

    This can use jinja2 placeholders:

    - `{{ client }}` the client object.
    - `{{ code }}` the code object.
    - `{{ calc }}` the calcjob object.

    """

    upload_paths: Mapped[Dict[str, Optional[str]]] = mapped_column(
        MutableDict.as_mutable(JSON()), default=dict
    )
    """Paths to upload to the remote machine: {path: key},
    relative to the work directory.

    - `path` POSIX formatted.
    - `key` pointing to the file in the object store, or None if a directory.
    """

    calcjobs: Mapped[List["CalcJob"]] = relationship("CalcJob")
    """The calcjobs that are associated with this code."""


class CalcJob(Base):
    """Input data for a single calculation job."""

    __tablename__ = "calcjob"

    label: Mapped[str] = mapped_column(default="")

    uuid: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid4()))
    """The unique identifier, for remote folder creation."""

    code_pk: Mapped[int] = mapped_column(ForeignKey("code.pk"))
    """The primary key of the code that this calcjob is associated with."""
    code: Mapped[Code] = relationship("Code", back_populates="calcjobs")
    """The code that this calcjob is associated with."""

    parameters: Mapped[Dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON()), default=dict
    )
    """JSONable data to store on the node."""

    upload: Mapped[Dict[str, Optional[str]]] = mapped_column(
        MutableDict.as_mutable(JSON()), default=dict
    )
    """Paths to upload to the remote machine: {path: key},
    relative to the work directory.

    - `path` POSIX formatted path.
    - `key` pointing to the file in the object store, or None if a directory.
    """

    download_globs: Mapped[List[str]] = mapped_column(
        MutableList.as_mutable(JSON()), default=list
    )
    """Globs to download from the remote machine to the object store,
    relative to the work directory.
    """

    status: Mapped["Processing"] = relationship(
        "Processing", single_parent=True, cascade="all, delete-orphan"
    )
    """The processing status of the calcjob."""

    outputs: Mapped[List["DataNode"]] = relationship(
        "DataNode", cascade="all, delete-orphan"
    )
    """The outputs of the calcjob."""

    @property
    def remote_path(self) -> Union[PurePosixPath, PureWindowsPath]:
        """Return the remote path for the calcjob execution."""
        return self.code.client.work_path / "workflows" / self.uuid


class Processing(Base):
    """The processing status of a single running calcjob."""

    __tablename__ = "calcjob_status"

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.pk}, calc={self.calcjob_pk})"

    calcjob_pk: Mapped[int] = mapped_column(ForeignKey("calcjob.pk"))
    """The primary key of the calculation that this status is associated with."""
    calcjob: Mapped[CalcJob] = relationship("CalcJob", back_populates="status")
    """The calcjob that this status is associated with."""

    step: Mapped[
        Literal[
            "created", "uploading", "submitting", "running", "retrieving", "finalised"
        ]
    ] = mapped_column(
        Enum(
            "created", "uploading", "submitting", "running", "retrieving", "finalised"
        ),
        default="created",
    )
    """The step of the calcjob."""

    job_id: Mapped[Optional[str]]
    """The job id of the calcjob, set by the scheduler."""

    exception: Mapped[Optional[str]]
    """The exception that was raised, if any."""


class DataNode(Base):
    """Data node to input or output from a calcjob."""

    __tablename__ = "data"

    attributes: Mapped[Dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON()), default=dict
    )
    """JSONable data to store on the node."""

    # TODO allow for this to not be set?
    creator_pk: Mapped[int] = mapped_column(ForeignKey("calcjob.pk"))
    """The primary key of the calcjob that created this node."""
    creator: Mapped[CalcJob] = relationship("CalcJob", back_populates="outputs")
    """The calcjob that created this node."""


NAMES: Tuple[str, ...] = (
    "digital_dynamo",
    "futuristic_fusion",
    "optical_odyssey",
    "radiant_rocket",
    "super_sonic",
    "crystal_cruiser",
    "creative_cyber",
    "efficient_explorer",
    "virtual_venture",
    "nifty_navigator",
    "glorious_galaxy",
    "optimized_operations",
    "astonishing_adventure",
    "elegant_evolution",
    "smooth_symphony",
    "powerful_prodigy",
    "virtual_visionary",
    "sleek_sentinel",
    "energetic_explorer",
    "optimistic_odyssey",
    "fantastic_frontier",
    "digital_dominion",
    "efficient_evolution",
    "virtual_voyager",
    "nimble_navigator",
    "glorious_gateway",
    "optimized_operations",
    "astonishing_array",
    "elegant_enterprise",
    "sophisticated_symphony",
    "perfect_prodigy",
    "virtual_victory",
    "speedy_sentinel",
    "energetic_enterprise",
    "optimistic_optimizer",
    "futuristic_fortune",
    "dynamic_dynamo",
    "flawless_fusion",
    "optimal_odyssey",
    "radiant_realm",
    "superior_symphony",
    "crystal_crusader",
    "creative_computing",
    "efficient_exec",
    "virtual_vision",
    "nifty_network",
    "glorious_grid",
    "optimized_optimizer",
    "astonishing_accelerator",
    "elegant_explorer",
)

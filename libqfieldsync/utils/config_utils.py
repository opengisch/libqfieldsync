import json
from collections.abc import Callable
from copy import deepcopy
from enum import Enum
from typing import (
    Any,
    Generic,
    Optional,
    Protocol,
    TypeVar,
    Union,
    cast,
    overload,
)

from qgis.core import QgsProject

T = TypeVar("T")


class ObjectInterface(Protocol):
    prefix: str
    project: QgsProject


class Field(Generic[T]):
    def __init__(self, value_type: type[T], name: str, default: Any = None) -> None:
        self.value_type = value_type
        self.entry_name = name
        self.default = default

        if self.default is None:
            if self.value_type is list:
                self.default = []
        else:
            if callable(self.default):
                default_value = self.default()
            else:
                default_value = self.default

            assert isinstance(default_value, self.value_type)

    @overload
    def __get__(self, obj: None, owner: Any = None) -> "Field[T]": ...

    @overload
    def __get__(self, obj: "ObjectInterface", owner: Any = None) -> T: ...

    def __get__(  # noqa: PLR0912
        self, obj: Optional["ObjectInterface"], owner: Any = None
    ) -> Union["Field[T]", T]:
        if obj is None:
            return self

        default: Any
        if self.value_type is bool:
            default = self._get_default()

            value, _ = obj.project.readBoolEntry(obj.prefix, self.entry_name, default)
        elif self.value_type is float:
            default = self._get_default()

            value, _ = obj.project.readDoubleEntry(obj.prefix, self.entry_name, default)
        elif self.value_type is int:
            default = self._get_default()

            if default is None:
                value, _ = obj.project.readNumEntry(obj.prefix, self.entry_name)
            else:
                value, _ = obj.project.readNumEntry(
                    obj.prefix, self.entry_name, default
                )
        elif self.value_type is str:
            default = self._get_default()

            value, _ = obj.project.readEntry(obj.prefix, self.entry_name, default or "")
        elif issubclass(self.value_type, Enum):
            default = self._get_default()

            if default is None:
                default_value = None
            else:
                default_value = default.value

            if issubclass(self.value_type, str):
                value, _ = obj.project.readEntry(
                    obj.prefix, self.entry_name, default_value
                )
            else:
                default_value = cast("int", default_value)

                value, _ = obj.project.readNumEntry(
                    obj.prefix, self.entry_name, default_value
                )

            value = self.value_type(value)
        elif self.value_type is list:
            default = self._get_default()

            value, _ = obj.project.readListEntry(obj.prefix, self.entry_name, default)
        else:
            default = self._get_default()

            if default is None:
                default_entry_value = None
            else:
                default_entry_value = json.dumps(default)

            encoded_value, _ = obj.project.readEntry(
                obj.prefix, self.entry_name, default_entry_value
            )

            if encoded_value is None or encoded_value == "":
                value = default
            else:
                value = json.loads(encoded_value)

        return cast("T", value)

    def __set__(self, obj: "ObjectInterface", value: T) -> None:  # noqa: PLR0912
        if value is None:
            obj.project.writeEntry(obj.prefix, self.entry_name, value)
            return

        if self.value_type is bool:
            if not isinstance(value, bool):
                raise TypeError(
                    f"Value for {self.entry_name} must be of type {self.value_type}, but got {type(value)}!"
                )

            obj.project.writeEntryBool(obj.prefix, self.entry_name, value)
        elif self.value_type is int:
            if not isinstance(value, int):
                raise TypeError(
                    f"Value for {self.entry_name} must be of type {self.value_type}, but got {type(value)}!"
                )

            obj.project.writeEntry(obj.prefix, self.entry_name, value)
        elif self.value_type is float:
            if not isinstance(value, float):
                raise TypeError(
                    f"Value for {self.entry_name} must be of type {self.value_type}, but got {type(value)}!"
                )

            obj.project.writeEntryDouble(obj.prefix, self.entry_name, value)
        elif self.value_type is str:
            if not isinstance(value, str):
                raise TypeError(
                    f"Value for {self.entry_name} must be of type {self.value_type}, but got {type(value)}!"
                )

            obj.project.writeEntry(obj.prefix, self.entry_name, value)
        elif issubclass(self.value_type, Enum):
            if not isinstance(value, self.value_type):
                raise TypeError(
                    f"Value for {self.entry_name} must be of type {self.value_type}, but got {type(value)}!"
                )

            obj.project.writeEntry(obj.prefix, self.entry_name, value.value)
        elif self.value_type is list:
            if not isinstance(value, list):
                raise TypeError(
                    f"Value for {self.entry_name} must be of type {self.value_type}, but got {type(value)}!"
                )

            obj.project.writeEntry(obj.prefix, self.entry_name, value)
        else:
            entry_value: Optional[str]

            if value is None:
                entry_value = value
            else:
                entry_value = json.dumps(value)

            obj.project.writeEntry(obj.prefix, self.entry_name, entry_value)

    def _get_default(self) -> Optional[T]:
        if callable(self.default):
            default = self.default()
        else:
            default = deepcopy(self.default)

        if default is None:
            return None

        assert isinstance(default, self.value_type)

        return cast("T", default)


@overload
def pfield(value_type: type[T], name: str) -> Field[Optional[T]]: ...


@overload
def pfield(value_type: type[T], name: str, default: Callable[[], T]) -> Field[T]: ...


@overload
def pfield(value_type: type[T], name: str, default: T) -> Field[T]: ...


def pfield(value_type: Any, name: str, default: Any = None) -> Field[Any]:
    """Convenience function for defining project fields with less boilerplate."""
    return Field(value_type, name, default)

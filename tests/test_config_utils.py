from enum import Enum

import pytest
from qgis.core import QgsProject
from qgis.testing import start_app

from libqfieldsync.utils.config_utils import Field, pfield

start_app()


class StringEnum(str, Enum):
    FOO = "foo"
    BAR = "bar"


class NumericEnum(Enum):
    ONE = 1
    TWO = 2


class StringifiedValue:
    def __str__(self) -> str:
        return "serialized-value"


class ConfigUnderTest:
    def __init__(self, project: QgsProject) -> None:
        self.project = project
        self.prefix = "qfieldsync"

    bool_value = pfield(bool, "bool_value", False)
    float_value = pfield(float, "float_value", 1.5)
    int_value = pfield(int, "int_value", 3)
    list_value = pfield(list, "list_value")
    text_value = pfield(str, "text_value")
    text_with_default = pfield(str, "text_with_default", "fallback")
    string_enum_value = pfield(StringEnum, "string_enum_value", StringEnum.FOO)
    numeric_enum_value = pfield(NumericEnum, "numeric_enum_value", NumericEnum.ONE)
    passthrough_value = pfield(str, "passthrough_value", "unused")


@pytest.fixture
def project():
    project = QgsProject()
    project.clear()
    yield project
    project.clear()


@pytest.fixture
def config(project):
    return ConfigUnderTest(project)


@pytest.mark.parametrize(
    ("value_type", "default"),
    [
        (bool, "nope"),
        (float, 1),
        (int, 1.5),
        (list, "not-a-list"),
        (str, 12),
        (StringEnum, "foo"),
        (NumericEnum, "what"),
    ],
)
def test_field_rejects_invalid_defaults(value_type, default):
    with pytest.raises(AssertionError):
        Field(value_type, "invalid", default)


def test_list_field_without_default_gets_empty_list_default():
    list_field = Field(list, "items")

    assert list_field.default == []


def test_descriptor_access_on_class_returns_field():
    assert isinstance(ConfigUnderTest.bool_value, Field)


def test_bool_field_reads_from_qgs_project(project, config):
    project.writeEntryBool("qfieldsync", "bool_value", True)

    assert config.bool_value is True


def test_float_field_reads_from_qgs_project(project, config):
    project.writeEntryDouble("qfieldsync", "float_value", 2.5)

    assert config.float_value == 2.5


def test_int_field_reads_from_qgs_project(project, config):
    project.writeEntry("qfieldsync", "int_value", 8)

    assert config.int_value == 8


def test_list_field_reads_from_qgs_project(project, config):
    project.writeEntry("qfieldsync", "list_value", ["a", "b"])

    assert config.list_value == ["a", "b"]


def test_string_enum_field_reads_and_casts_to_enum(project, config):
    project.writeEntry("qfieldsync", "string_enum_value", "bar")

    assert config.string_enum_value is StringEnum.BAR


def test_numeric_enum_field_reads_and_casts_to_enum(project, config):
    project.writeEntry("qfieldsync", "numeric_enum_value", 2)

    assert config.numeric_enum_value is NumericEnum.TWO


def test_text_field_reads_plain_string(project, config):
    project.writeEntry("qfieldsync", "text_value", "hello")

    assert config.text_value == "hello"


def test_text_field_returns_default_when_entry_is_missing(config):
    assert config.text_with_default == "fallback"


def test_text_field_returns_empty_string_when_no_default_and_entry_is_missing(config):
    assert config.text_value == ""


def test_bool_field_writes_with_bool_writer(project, config):
    config.bool_value = True

    assert project.readBoolEntry("qfieldsync", "bool_value", False) == (True, True)
    assert config.bool_value is True


def test_float_field_writes_with_double_writer(project, config):
    config.float_value = 4.5

    assert project.readDoubleEntry("qfieldsync", "float_value", 0.0) == (4.5, True)
    assert config.float_value == 4.5


def test_int_field_writes_with_entry_writer(project, config):
    config.int_value = 10

    assert project.readNumEntry("qfieldsync", "int_value", 0) == (10, True)
    assert config.int_value == 10


def test_enum_field_writes_enum_values(project, config):
    config.string_enum_value = StringEnum.BAR
    config.numeric_enum_value = NumericEnum.TWO

    assert project.readEntry("qfieldsync", "string_enum_value", "") == ("bar", True)
    assert project.readNumEntry("qfieldsync", "numeric_enum_value", 0) == (2, True)
    assert config.string_enum_value is StringEnum.BAR
    assert config.numeric_enum_value is NumericEnum.TWO


def test_text_value_writes_through_entry_writer(project, config):
    config.text_value = "plain-text"

    assert project.readEntry("qfieldsync", "text_value", "fallback") == (
        "plain-text",
        True,
    )
    assert config.text_value == "plain-text"


def test_list_value_writes_through_list_storage(project, config):
    config.list_value = ["a", "b"]

    assert project.readListEntry("qfieldsync", "list_value", []) == (["a", "b"], True)
    assert config.list_value == ["a", "b"]


def test_bool_field_rejects_non_bool_value(config):
    with pytest.raises(TypeError):
        config.bool_value = "not-a-bool"


def test_numeric_enum_field_returns_default_when_entry_is_missing(config):
    assert config.numeric_enum_value is NumericEnum.ONE


def test_list_field_returns_empty_list_when_entry_is_missing(config):
    assert config.list_value == []


def test_none_value_is_stored_as_empty_string(project, config):
    config.passthrough_value = None

    assert project.readEntry("qfieldsync", "passthrough_value", "fallback") == (
        "",
        True,
    )
    assert config.passthrough_value == ""


def test_non_primitive_value_is_stringified_before_writing(project, config):
    config.passthrough_value = str(StringifiedValue())

    assert project.readEntry("qfieldsync", "passthrough_value", "fallback") == (
        "serialized-value",
        True,
    )
    assert config.passthrough_value == "serialized-value"


def test_dict_value_is_json_encoded(project):
    def default_dict():
        return {"key1": 1}

    class JsonEncodedConfig:
        def __init__(self, project: QgsProject) -> None:
            self.project = project
            self.prefix = "qfieldsync"

        without_default = pfield(dict, "without_default")
        with_default = pfield(dict, "with_default", default_dict)

    c = JsonEncodedConfig(project)

    assert c.without_default is None

    c.without_default = {"a": 1}

    assert c.without_default == {"a": 1}
    # Check that the raw stored value is JSON-encoded, not just stringified as a dict
    assert project.readEntry("qfieldsync", "without_default", "") == (
        '{"a": 1}',
        True,
    )

    assert c.with_default == default_dict()

    c.with_default = {"b": 2}

    assert c.with_default == {"b": 2}
    # Check that the raw stored value is JSON-encoded, not just stringified as a dict
    assert project.readEntry("qfieldsync", "with_default", "") == (
        '{"b": 2}',
        True,
    )


def test_mutable_default_value_is_deep_copied(project):
    default_value = {"nested": {"items": []}}
    expected_value = {"nested": {"items": []}}

    field = Field(dict, "mutable_default", default_value)

    first_value = field._get_default()
    assert first_value is not None
    first_value["nested"]["items"].append("changed")

    assert field._get_default() != first_value
    assert field._get_default() == expected_value

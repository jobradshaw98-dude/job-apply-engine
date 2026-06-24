import pytest
from apply_engine.field_map import map_field


@pytest.mark.parametrize("label,name,placeholder,expected", [
    ("First Name", "", "", "first_name"),
    ("Given name", "first_name", "", "first_name"),
    ("Last Name", "", "", "last_name"),
    ("Surname", "lastName", "", "last_name"),
    ("Full name", "name", "", "full_name"),
    ("Email", "", "you@example.com", "email"),
    ("Email Address", "email", "", "email"),
    ("Phone", "", "", "phone"),
    ("Mobile number", "phone", "", "phone"),
    ("LinkedIn Profile", "", "", "linkedin"),
    ("City", "", "", "city"),
    ("What is your expected salary?", "salary", "", None),
    ("Years of experience", "", "", None),
])
def test_map_field(label, name, placeholder, expected):
    assert map_field(label, name, placeholder, None) == expected


def test_full_name_not_confused_with_first():
    # "name" alone -> full_name, but "first name" -> first_name (first wins)
    assert map_field("First name", "name", "", None) == "first_name"

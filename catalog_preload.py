"""Read-only LabMate/Bhasin profile and child-test catalog preload.

This module intentionally reads only `test` and `testprofile`. It does not
load patient, booking, panel-rate, or price data.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pymysql


def load_local_env():
    """Load local development configuration without adding a dotenv dependency."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_local_env()


def catalog_connection():
    return pymysql.connect(
        host=os.getenv("CATALOG_DB_HOST", os.getenv("LABMATE_DB_HOST", "10.1.1.44")),
        port=int(os.getenv("CATALOG_DB_PORT", os.getenv("LABMATE_DB_PORT", "3306"))),
        user=os.getenv("CATALOG_DB_USER", os.getenv("LABMATE_DB_USER", "sahil")),
        password=os.environ.get("CATALOG_DB_PASSWORD", os.environ["LABMATE_DB_PASSWORD"]),
        database=os.getenv("CATALOG_DB_NAME", "bhasin_7001_new"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


def key_string(gcode, scode, test_code):
    return f"{gcode}|{scode}|{test_code}"


@dataclass(frozen=True)
class CatalogTest:
    gcode: str
    scode: str
    test_code: str
    booked_code: str
    name: str
    shortname: str
    is_profile: bool

    @property
    def key(self):
        return (self.gcode, self.scode, self.test_code)

    @property
    def key_string(self):
        return key_string(*self.key)

    def as_dict(self):
        return {
            "gcode": self.gcode,
            "scode": self.scode,
            "test_code": self.test_code,
            "booked_code": self.booked_code,
            "name": self.name,
            "shortname": self.shortname,
            "is_profile": self.is_profile,
        }


class CatalogPreload:
    def __init__(self, tests, tests_by_booked_code, profile_children, missing_parent_mappings, missing_child_mappings):
        self.tests = tests
        self.tests_by_booked_code = tests_by_booked_code
        self.profile_children = profile_children
        self.missing_parent_mappings = missing_parent_mappings
        self.missing_child_mappings = missing_child_mappings

    def as_dict(self):
        return {
            "tests": {key: test.as_dict() for key, test in self.tests.items()},
            "profile_children": {
                key: [child.as_dict() for child in children]
                for key, children in self.profile_children.items()
            },
        }

    def get_descendants(self, parent_key):
        """Return unique direct and nested children, safely handling profile cycles."""
        if isinstance(parent_key, str):
            parent_key = tuple(parent_key.split("|", 2))
        root_key = key_string(*parent_key)
        visited_parents = set()
        seen_booked_codes = {self.tests[root_key].booked_code} if root_key in self.tests else set()
        descendants = []

        def walk(key):
            normalized_key = key_string(*key)
            if normalized_key in visited_parents:
                return
            visited_parents.add(normalized_key)
            for child in self.profile_children.get(normalized_key, []):
                if child.booked_code not in seen_booked_codes:
                    seen_booked_codes.add(child.booked_code)
                    descendants.append(child)
                if child.is_profile:
                    walk(child.key)

        walk(parent_key)
        return descendants

    def get_test_by_booked_code(self, booked_code):
        """Return a catalog test by `test.Testcode1` / booked test code."""
        return self.tests_by_booked_code.get(str(booked_code or "").strip())


@lru_cache(maxsize=1)
def load_catalog_preload():
    """Load profile parents and their children using only read-only SELECT statements."""
    tests_by_gst = {}
    tests_by_code1 = {}

    with catalog_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT Gcode, Scode, TestCode, Testcode1, Description, Shortname, Profile
                FROM test
                """
            )
            for row in cur.fetchall():
                test = CatalogTest(
                    gcode=str(row["Gcode"] or "").strip(),
                    scode=str(row["Scode"] or "").strip(),
                    test_code=str(row["TestCode"] or "").strip(),
                    booked_code=str(row["Testcode1"] or "").strip(),
                    name=str(row["Description"] or "").strip(),
                    shortname=str(row["Shortname"] or "").strip(),
                    is_profile=bool(row["Profile"]),
                )
                tests_by_gst.setdefault(test.key, test)
                if test.booked_code:
                    tests_by_code1.setdefault(test.booked_code, test)

            cur.execute(
                """
                SELECT Gcode, SCode, ProfileCode, TestCode
                FROM testprofile
                """
            )
            mapping_rows = cur.fetchall()

    profile_children = {}
    seen_children = {}
    missing_parent_mappings = []
    missing_child_mappings = []
    for mapping in mapping_rows:
        parent_key = (
            str(mapping["Gcode"] or "").strip(),
            str(mapping["SCode"] or "").strip(),
            str(mapping["ProfileCode"] or "").strip(),
        )
        child_booked_code = str(mapping["TestCode"] or "").strip()
        parent = tests_by_gst.get(parent_key)
        child = tests_by_code1.get(child_booked_code)
        if not parent:
            missing_parent_mappings.append(parent_key)
            continue
        if not child:
            missing_child_mappings.append((parent_key, child_booked_code))
            continue

        parent_key_string = key_string(*parent_key)
        children = profile_children.setdefault(parent_key_string, [])
        parent_seen = seen_children.setdefault(parent_key_string, set())
        if child.booked_code not in parent_seen:
            children.append(child)
            parent_seen.add(child.booked_code)

    tests = {key_string(*key): test for key, test in tests_by_gst.items()}
    return CatalogPreload(
        tests,
        tests_by_code1,
        profile_children,
        missing_parent_mappings,
        missing_child_mappings,
    )


def refresh_catalog_preload():
    """Clear the process-local preload cache and reload the catalog on demand."""
    load_catalog_preload.cache_clear()
    return load_catalog_preload()


if __name__ == "__main__":
    catalog = load_catalog_preload()
    print(
        {
            "tests": len(catalog.tests),
            "profiles_with_children": len(catalog.profile_children),
            "missing_parent_mappings": len(catalog.missing_parent_mappings),
            "missing_child_mappings": len(catalog.missing_child_mappings),
        }
    )

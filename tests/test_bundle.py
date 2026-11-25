import os
import sys
import tempfile
import unittest
from unittest import mock

from rbridge import bundle


class ProjectDirTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.project = os.path.join(self.root, "app")
        os.makedirs(os.path.join(self.project, "src", "deep"))
        self.venv = os.path.join(self.project, ".venv")

    def in_venv(self, prefix):
        return mock.patch.multiple(sys, prefix=prefix, base_prefix="/usr")

    def test_outside_a_virtualenv_is_global(self):
        with mock.patch.multiple(sys, prefix="/usr", base_prefix="/usr"):
            self.assertIsNone(bundle.project_dir(self.project))

    def test_venv_inside_the_folder_being_worked_in(self):
        with self.in_venv(self.venv):
            self.assertEqual(bundle.project_dir(self.project), self.project)
            self.assertEqual(bundle.project_dir(os.path.join(self.project, "src", "deep")), self.project)

    def test_venv_elsewhere_uses_the_nearest_marker(self):
        open(os.path.join(self.project, "pyproject.toml"), "w").close()
        with self.in_venv(os.path.join(self.root, "cache", "venvs", "app-1")):
            self.assertEqual(bundle.project_dir(os.path.join(self.project, "src", "deep")), self.project)
            lone = os.path.join(self.root, "lone")
            os.makedirs(lone)
            self.assertEqual(bundle.project_dir(lone), lone)

    def test_home_only_with_a_bundled_binary_and_never_over_rbridge_home(self):
        with self.in_venv(self.venv), mock.patch("os.getcwd", return_value=self.project):
            with mock.patch.object(bundle, "bundled_binary", return_value=None):
                self.assertIsNone(bundle.project_home())
                self.assertNotIn("RBRIDGE_HOME", bundle.effective_env({}))
            with mock.patch.object(bundle, "bundled_binary", return_value="/x/rbridge"):
                self.assertEqual(bundle.effective_env({})["RBRIDGE_HOME"], os.path.join(self.project, ".rbridge"))
                self.assertEqual(bundle.effective_env({"RBRIDGE_HOME": "/chosen"})["RBRIDGE_HOME"], "/chosen")

    def test_find_binary_order(self):
        with mock.patch.object(bundle, "bundled_binary", return_value="/bundled/rbridge"):
            self.assertEqual(bundle.find_binary({"RBRIDGE_BIN": "/mine/rbridge"}), "/mine/rbridge")
            self.assertEqual(bundle.find_binary({"PATH": ""}), "/bundled/rbridge")


if __name__ == "__main__":
    unittest.main()

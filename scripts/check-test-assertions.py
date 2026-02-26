#!/usr/bin/env python3
"""
Pre-commit hook to detect tests without assertions.

This script uses AST parsing to find test functions and verify they contain
assertion statements or expected patterns (pytest.raises with exc_info, etc.).

Usage:
    check-test-assertions.py [file1.py file2.py ...]

    If no files provided, checks all staged test files.

Exit codes:
    0: All tests have assertions
    1: One or more tests lack assertions
"""

import ast
import os
import sys
from pathlib import Path
from typing import Optional


class TestAssertionChecker(ast.NodeVisitor):
    """AST visitor to check if test functions contain assertions."""

    def __init__(self):
        self.has_assertion = False
        self.test_functions: list[tuple[str, int, bool]] = (
            []
        )  # (name, line, has_assertion)
        self.current_test: str | None = None
        self.current_line: int = 0

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Visit function definitions to find test functions."""
        # Check if this is a test function
        is_test = (
            node.name.startswith("test_")
            or node.name.endswith("_test")
            or any(
                isinstance(dec, ast.Name) and dec.id == "pytest.mark.parametrize"
                for dec in node.decorator_list
            )
        )

        if is_test:
            # Reset for this test
            self.current_test = node.name
            self.current_line = node.lineno
            self.has_assertion = False

            # Visit all nodes in this function
            for child in ast.walk(node):
                if self._has_assertion_pattern(child):
                    self.has_assertion = True
                    break

            # Record result
            self.test_functions.append((node.name, node.lineno, self.has_assertion))

        # Continue visiting
        self.generic_visit(node)

    def _has_assertion_pattern(self, node: ast.AST) -> bool:
        """Check if a node represents an assertion pattern."""
        # Pattern 1: Direct assert statement
        if isinstance(node, ast.Assert):
            return True

        # Pattern 2: pytest.raises with exc_info (assigns to variable)
        if isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    func = item.context_expr.func

                    # Check for pytest.raises
                    if isinstance(func, ast.Attribute) and func.attr == "raises":
                        # If assigns to variable, it's likely checking exception
                        if item.optional_vars is not None:
                            return True

                    # Check for unittest.mock.patch or similar
                    if isinstance(func, ast.Attribute) and func.attr in (
                        "patch",
                        "patch.object",
                    ):
                        # Mock patches often used with assertions
                        return True

        # Pattern 3: unittest assertions (assertEqual, assertTrue, etc.)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr.startswith("assert"):
                    return True

        # Pattern 4: pytest.fail() - explicit failure
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "fail":
                if isinstance(func.value, ast.Name) and func.value.id == "pytest":
                    return True

        return False


def find_test_files(paths: list[str] = None) -> list[str]:
    """Find test files to check."""
    if paths:
        # Use provided paths
        test_files = []
        for path in paths:
            if os.path.isfile(path) and path.endswith(".py"):
                # Check if it's a test file
                filename = os.path.basename(path)
                if filename.startswith("test_") or filename.endswith("_test.py"):
                    test_files.append(path)
        return test_files

    # Get staged files from git
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            check=True,
        )
        staged_files = result.stdout.strip().split("\n")

        # Filter to test files
        test_files = [
            f
            for f in staged_files
            if f.endswith(".py")
            and (
                os.path.basename(f).startswith("test_")
                or os.path.basename(f).endswith("_test.py")
                or "test" in os.path.dirname(f).lower()
            )
        ]
        return test_files
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Git not available or not in a git repo - check current directory
        test_files = []
        for root, dirs, files in os.walk("."):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for file in files:
                if file.startswith("test_") and file.endswith(".py"):
                    test_files.append(os.path.join(root, file))
        return test_files


def check_file(filepath: str) -> tuple[bool, list[tuple[str, int]]]:
    """
    Check a single file for tests without assertions.

    Returns:
        (all_have_assertions, list_of_tests_without_assertions)
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()

        tree = ast.parse(content, filename=filepath)
        checker = TestAssertionChecker()
        checker.visit(tree)

        # Find tests without assertions
        tests_without = [
            (name, line)
            for name, line, has_assert in checker.test_functions
            if not has_assert
        ]

        return len(tests_without) == 0, tests_without

    except SyntaxError as e:
        print(f"⚠️  Syntax error in {filepath}:{e.lineno}: {e.msg}", file=sys.stderr)
        # Don't fail on syntax errors - let other tools handle that
        return True, []
    except Exception as e:
        print(f"❌ Error checking {filepath}: {e}", file=sys.stderr)
        return False, []


def main():
    """Main entry point."""
    # Get files to check
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
        files = find_test_files()

    if not files:
        # No test files to check
        sys.exit(0)

    all_passed = True
    failed_tests: list[tuple[str, str, int]] = []  # (file, test_name, line)

    for filepath in files:
        if not os.path.exists(filepath):
            continue

        passed, tests_without = check_file(filepath)

        if not passed:
            all_passed = False
            for test_name, line in tests_without:
                failed_tests.append((filepath, test_name, line))

    # Report results
    if not all_passed:
        print("❌ Tests without assertions detected:", file=sys.stderr)
        print("", file=sys.stderr)

        for filepath, test_name, line in failed_tests:
            print(f"  {filepath}:{line} - {test_name}()", file=sys.stderr)

        print("", file=sys.stderr)
        print(
            "💡 All test functions must contain at least one assertion.",
            file=sys.stderr,
        )
        print("   Common patterns:", file=sys.stderr)
        print("   - assert statements", file=sys.stderr)
        print("   - pytest.raises(...) with variable assignment", file=sys.stderr)
        print(
            "   - unittest assertions (assertEqual, assertTrue, etc.)", file=sys.stderr
        )
        print("   - pytest.fail()", file=sys.stderr)

        sys.exit(1)

    print("✅ All tests have assertions")
    sys.exit(0)


if __name__ == "__main__":
    main()

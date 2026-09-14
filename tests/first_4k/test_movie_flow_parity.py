"""`MovieFlow` now exists twice; these tests prove the copies still say the same thing.

`scripts/first_4k_movie.py` is frozen while a run is in flight -- a supervisor
re-executes it from disk between segments, so editing it mid-run is how a resume
turns into BLOCKED. Stage A therefore copies its 785-line `MovieFlow` into
`pixav.first_4k` as mixins and leaves the original untouched.

A copy that can be edited on either side silently is the failure this whole
branch exists to remove, and a *split* copy cannot be checked with a file digest
the way `contracts.py` and `recovery.py` are. So the comparison is per function:
each method's exact source, on both sides, after applying a short list of
declared divergences. A divergence whose anchor stops matching fails too, which
is what keeps the list honest.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT / "scripts/first_4k_movie.py"
PACKAGE = ROOT / "src/pixav/first_4k"

#: Which package class took each `MovieFlow` method.
METHODS = {
    "prepare.py::PrepareMixin": [
        "recover_prepared",
        "discover",
        "download_prepare",
        "stop_candidate",
        "torrent_file",
        "start_download",
        "download_budget",
        "_candidate",
    ],
    "upload.py::UploadMixin": ["upload", "pause_after", "quota_wait"],
    "playback.py::PlaybackMixin": ["playback", "verify"],
    "flow.py::MovieFlow": ["__init__", "image_id", "check_operation", "save", "reset", "runtime"],
}

#: Module-level names, and where each one landed.
FUNCTIONS = {
    "settings.py": ["now", "container"],
    "cli.py": [
        "preflight",
        "checked_database",
        "runtime_configuration",
        "dispatch",
        "execute",
        "nonnegative_int",
        "main",
    ],
}

#: Every intended difference, and why it exists. Anything else is drift.
DIVERGENCES = [
    (
        "MovieTorrent",
        "    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:\n",
        "    async def __aenter__(self) -> MovieTorrent:\n"
        "        # QBitClient.__aenter__ is annotated as returning QBitClient, so\n"
        "        # `async with MovieTorrent(...)` would lose this subclass and with it\n"
        "        # the check/progress latches. Same object, narrower type.\n"
        "        await super().__aenter__()\n"
        "        return self\n"
        "\n"
        "    # The supertype takes *_exc_info; three named arguments are the protocol's\n"
        "    # own shape and what `async with` passes, but mypy sees a narrowing.\n"
        "    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:  # type: ignore[override]\n",
    ),
    (
        "execute",
        "    client = docker.from_env()\n",
        "    client = cast(Any, docker).from_env()\n",
    ),
]


def read(path: Path) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Exact source text of every top-level definition and method in ``path``."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    def source(node: ast.AST) -> str:
        first = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])  # type: ignore[attr-defined]
        return "".join(lines[first - 1 : node.end_lineno])  # type: ignore[attr-defined]

    top: dict[str, str] = {}
    methods: dict[str, dict[str, str]] = {}
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top[node.name] = source(node)
        if isinstance(node, ast.ClassDef):
            methods[node.name] = {
                sub.name: source(sub) for sub in node.body if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return top, methods


def expected(symbol: str, original: str) -> str:
    """The original source with this symbol's declared divergences applied."""
    for name, before, after in DIVERGENCES:
        if name == symbol:
            assert before in original, f"declared divergence for {symbol} no longer matches the original"
            original = original.replace(before, after)
    return original


@pytest.fixture(scope="module")
def frozen() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    if not ORIGINAL.exists() or "from pixav.first_4k" in ORIGINAL.read_text(encoding="utf-8"):
        pytest.skip("stage B has replaced the script with a shim; there is no second copy left")
    return read(ORIGINAL)


class TestMethodsMatch:
    @pytest.mark.parametrize(
        ("location", "method"),
        [(location, method) for location, names in METHODS.items() for method in names],
    )
    def test_the_package_method_is_the_frozen_method(
        self, location: str, method: str, frozen: tuple[dict[str, str], dict[str, dict[str, str]]]
    ) -> None:
        filename, classname = location.split("::")
        _, package = read(PACKAGE / filename)
        _, original = frozen

        assert method in package[classname], f"{classname} lost {method}"
        assert package[classname][method] == expected(method, original["MovieFlow"][method]), (
            f"{filename}::{classname}.{method} no longer matches scripts/first_4k_movie.py. "
            "Edit one copy and you must edit the other, or finish stage B and delete the original."
        )

    def test_every_frozen_method_has_exactly_one_home(
        self, frozen: tuple[dict[str, str], dict[str, dict[str, str]]]
    ) -> None:
        """A method dropped during the split would otherwise just be missing."""
        _, original = frozen
        placed = [name for names in METHODS.values() for name in names]

        assert sorted(placed) == sorted(original["MovieFlow"]), "the split lost or duplicated a method"
        assert len(placed) == len(set(placed))


class TestFunctionsMatch:
    @pytest.mark.parametrize(
        ("filename", "name"),
        [(filename, name) for filename, names in FUNCTIONS.items() for name in names] + [("torrent.py", "MovieTorrent")],
    )
    def test_the_package_definition_is_the_frozen_definition(
        self, filename: str, name: str, frozen: tuple[dict[str, str], dict[str, dict[str, str]]]
    ) -> None:
        package, _ = read(PACKAGE / filename)
        original, _ = frozen

        assert name in package, f"{filename} lost {name}"
        assert package[name] == expected(name, original[name]), (
            f"{filename}::{name} no longer matches scripts/first_4k_movie.py."
        )


class TestPackageShape:
    @pytest.mark.parametrize("module", sorted(path.name for path in PACKAGE.glob("*.py")))
    def test_each_module_stays_under_the_project_file_limit(self, module: str) -> None:
        """`.claude/rules/common/coding-style.md`: keep files under 400 lines."""
        assert len((PACKAGE / module).read_text(encoding="utf-8").splitlines()) <= 400

    def test_the_package_never_carries_the_isolated_database_password(self) -> None:
        """The original hardcodes a DSN. The package reads it from the environment."""
        for path in PACKAGE.glob("*.py"):
            body = path.read_text(encoding="utf-8")
            assert "postgresql://" not in body, f"{path.name} hardcodes a DSN"
            assert "first-4k-isolated" not in body, f"{path.name} hardcodes the isolated password"

    def test_the_package_does_not_import_back_out_of_itself(self) -> None:
        """A package that still needs `scripts/` has not actually been consolidated."""
        for path in PACKAGE.glob("*.py"):
            assert "from scripts" not in path.read_text(encoding="utf-8"), f"{path.name} imports from scripts/"

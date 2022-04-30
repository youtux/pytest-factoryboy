from __future__ import annotations

import atexit
import importlib.util
import itertools
import logging
import pathlib
import shutil
import tempfile
import typing
from dataclasses import field, dataclass
from functools import lru_cache
from types import ModuleType

import mako.template
from appdirs import AppDirs

cache_dir = pathlib.Path(AppDirs("pytest-factoryboy").user_cache_dir)

logger = logging.getLogger(__name__)


@dataclass
class FixtureDef:
    name: str
    function_name: typing.Literal["model_fixture", "attr_fixture", "factory_fixture", "subfactory_fixture"]
    function_kwargs: dict = field(default_factory=dict)
    deps: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)

    @property
    def kwargs_var_name(self):
        return f"_{self.name}__kwargs"


module_template = mako.template.Template(
    """\
import pytest
from pytest_factoryboy.fixture import (
    attr_fixture,
    factory_fixture,
    model_fixture,
    subfactory_fixture,
)


def _fixture(related):
    def fixture_maker(fn):
        fn._factoryboy_related = related
        return pytest.fixture(fn)

    return fixture_maker
% for fixture_def in fixture_defs:


${ fixture_def.kwargs_var_name } = {}


@_fixture(related=${ repr(fixture_def.related) })
def ${ fixture_def.name }(
% for dep in ["request"] + fixture_def.deps:
    ${ dep },
% endfor
):
    return ${ fixture_def.function_name }(request, **${ fixture_def.kwargs_var_name })
% endfor
"""
)

init_py_content = '''\
"""Pytest-factoryboy generated fixtures.

This module and the other modules in this package are automatically generated by
pytest-factoryboy. They will be rewritten on the next run.

"""
'''


@lru_cache()  # This way we reuse the same folder for the whole execution of the program
def make_temp_folder(package_name: str) -> pathlib.Path:
    """Create a temporary folder and automatically delete it when the process exit."""
    path = pathlib.Path(tempfile.mkdtemp()) / package_name
    path.mkdir(parents=True, exist_ok=True)

    atexit.register(shutil.rmtree, str(path))

    return path


@lru_cache()  # This way we reuse the same folder for the whole execution of the program
def create_package(package_name: str, init_py_content=init_py_content) -> pathlib.Path:
    path = cache_dir / package_name
    try:
        if path.exists():
            shutil.rmtree(str(path))

        path.mkdir(parents=True, exist_ok=False)
    except OSError:  # Catch cases where the directory can't be removed or can't be created
        logger.warning(f"Can't create the cache directory {path}. Using a temporary directory instead.", exc_info=True)
        return make_temp_folder(package_name)

    (path / "__init__.py").write_text(init_py_content)

    return path


def make_module(code: str, module_name: str, package_name: str) -> ModuleType:
    tmp_module_path = create_package(package_name) / f"{module_name}.py"

    counter = itertools.count(1)
    while tmp_module_path.exists():
        count = next(counter)
        new_stem = f"{tmp_module_path.stem}_{count}"
        tmp_module_path = tmp_module_path.with_stem(new_stem)

    logger.info(f"Writing content of {module_name!r} into {tmp_module_path}.")

    tmp_module_path.write_text(code)

    spec = importlib.util.spec_from_file_location(f"{package_name}.{module_name}", tmp_module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fixture_model_module(model_name, fixture_defs: list[FixtureDef]):
    code = module_template.render(fixture_defs=fixture_defs)
    generated_module = make_module(code, module_name=model_name, package_name="_pytest_factoryboy_generated_fixtures")
    for fixture_def in fixture_defs:
        assert hasattr(generated_module, fixture_def.kwargs_var_name)
        setattr(generated_module, fixture_def.kwargs_var_name, fixture_def.function_kwargs)
    return generated_module

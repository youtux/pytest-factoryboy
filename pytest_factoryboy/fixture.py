"""Factory boy fixture integration."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from inspect import signature
from typing import TYPE_CHECKING, cast, overload

import factory
import factory.builder
import factory.declarations
import factory.enums
import inflection
from factory.declarations import NotProvided

from .codegen import FixtureDef, make_fixture_model_module
from .compat import PostGenerationContext

if TYPE_CHECKING:
    from typing import Any, Callable, Iterable, Mapping, TypeAlias, TypeVar

    from _pytest.fixtures import FixtureFunction, SubRequest
    from factory.builder import BuildStep
    from factory.declarations import PostGeneration, PostGenerationContext

    from .plugin import Request as FactoryboyRequest

    FactoryType: TypeAlias = type[factory.Factory]
    T = TypeVar("T")
    F = TypeVar("F", bound=FactoryType)


SEPARATOR = "__"


@dataclass(eq=False)
class DeferredFunction:
    name: str
    factory: FactoryType
    is_related: bool
    function: Callable[[SubRequest], Any]

    def __call__(self, request: SubRequest) -> Any:
        return self.function(request)


# register(AuthorFactory, ...)
#
# @register
# class AuthorFactory(factory.Factory): ...
@overload
def register(factory_class: F, _name: str | None = None, **kwargs: Any) -> F:
    ...


# @register(...)
# class AuthorFactory(factory.Factory): ...
@overload
def register(*, _name: str | None = None, **kwargs: Any) -> Callable[[F], F]:
    ...


def register(
    factory_class: F | None = None,
    _name: str | None = None,
    *,
    _caller_locals: dict[str, Any] | None = None,
    **kwargs: Any,
) -> F | Callable[[F], F]:
    r"""Register fixtures for the factory class.

    :param factory_class: Factory class to register.
    :param _name: Name of the model fixture. By default, is lowercase-underscored model name.
    :param _caller_locals: Dictionary where to inject the generated fixtures. Defaults to the caller's locals().
    :param \**kwargs: Optional keyword arguments that override factory attributes.
    """
    if _caller_locals is None:
        _caller_locals = get_caller_locals()

    if factory_class is None:

        def register_(factory_class: F) -> F:
            return register(factory_class, _name=_name, _caller_locals=_caller_locals, **kwargs)

        return register_

    assert not factory_class._meta.abstract, "Can't register abstract factories."
    assert factory_class._meta.model is not None, "Factory model class is not specified."

    model_name = get_model_name(factory_class) if _name is None else _name

    fixture_defs = list(
        generate_fixturedefs(
            factory_class=factory_class, model_name=model_name, overrides=kwargs, caller_locals=_caller_locals
        )
    )

    generated_module = make_fixture_model_module(model_name, fixture_defs)

    for fixture_def in fixture_defs:
        exported_name = fixture_def.name
        fixture_function = getattr(generated_module, exported_name)
        inject_into_caller(exported_name, fixture_function, _caller_locals)

    return factory_class


def generate_fixturedefs(
    factory_class: FactoryType, model_name: str, overrides: Mapping[str, Any], caller_locals: Mapping[str, Any]
) -> Iterable[FixtureDef]:
    """Generate all the FixtureDefs for the given factory class."""
    factory_name = get_factory_name(factory_class)

    related: list[str] = []
    for attr, value in factory_class._meta.declarations.items():
        value = overrides.get(attr, value)
        attr_name = SEPARATOR.join((model_name, attr))
        yield (
            make_declaration_fixturedef(
                attr_name=attr_name,
                value=value,
                factory_class=factory_class,
                related=related,
            )
        )

    if factory_name not in caller_locals:
        yield (
            FixtureDef(
                name=factory_name,
                function_name="factory_fixture",
                function_kwargs={"factory_class": factory_class},
            )
        )

    deps = get_deps(factory_class, model_name=model_name)
    yield (
        FixtureDef(
            name=model_name,
            function_name="model_fixture",
            function_kwargs={"factory_name": factory_name},
            deps=deps,
            related=related,
        )
    )


def make_declaration_fixturedef(
    attr_name: str,
    value: Any,
    factory_class: FactoryType,
    related: list[str],
) -> FixtureDef:
    """Create the FixtureDef for a factory declaration."""
    if isinstance(value, (factory.SubFactory, factory.RelatedFactory)):
        subfactory_class = value.get_factory()
        subfactory_deps = get_deps(subfactory_class, factory_class)

        args = list(subfactory_deps)
        if isinstance(value, factory.RelatedFactory):
            related_model = get_model_name(subfactory_class)
            args.append(related_model)
            related.append(related_model)
            related.append(attr_name)
            related.extend(subfactory_deps)

        if isinstance(value, factory.SubFactory):
            args.append(inflection.underscore(subfactory_class._meta.model.__name__))

        return FixtureDef(
            name=attr_name,
            function_name="subfactory_fixture",
            function_kwargs={"factory_class": subfactory_class},
            deps=args,
        )

    deps: list[str]  # makes mypy happy
    if isinstance(value, factory.PostGeneration):
        value = None
        deps = []
    elif isinstance(value, factory.PostGenerationMethodCall):
        value = value.method_arg
        deps = []
    elif isinstance(value, LazyFixture):
        value = value
        deps = value.args
    else:
        value = value
        deps = []

    return FixtureDef(
        name=attr_name,
        function_name="attr_fixture",
        function_kwargs={"value": value},
        deps=deps,
    )


def inject_into_caller(name: str, function: Callable[..., Any], locals_: dict[str, Any]) -> None:
    """Inject a function into the caller's locals, making sure that the function will work also within classes."""
    # We need to check if the caller frame is a class, since in that case the first argument is the class itself.
    # In that case, we can apply the staticmethod() decorator to the injected function, so that the first param
    # will be disregarded.
    # To figure out if the caller frame is a class, we can check if the __qualname__ attribute is present.

    # According to the python docs, __qualname__ is available for both **classes and functions**.
    # However, it seems that for functions it is not yet available in the function namespace before it's defined.
    # This could change in the future, but it shouldn't be too much of a problem since registering a factory
    # in a function namespace would not make it usable anyway.
    # Therefore, we can just check for __qualname__ to figure out if we are in a class, and apply the @staticmethod.
    is_class_or_function = "__qualname__" in locals_
    if is_class_or_function:
        function = staticmethod(function)

    locals_[name] = function


def get_model_name(factory_class: FactoryType) -> str:
    """Get model fixture name by factory."""
    return (
        inflection.underscore(factory_class._meta.model.__name__)
        if not isinstance(factory_class._meta.model, str)
        else factory_class._meta.model
    )


def get_factory_name(factory_class: FactoryType) -> str:
    """Get factory fixture name by factory."""
    return inflection.underscore(factory_class.__name__)


def get_deps(
    factory_class: FactoryType,
    parent_factory_class: FactoryType | None = None,
    model_name: str | None = None,
) -> list[str]:
    """Get factory dependencies.

    :return: List of the fixture argument names for dependency injection.
    """
    model_name = get_model_name(factory_class) if model_name is None else model_name
    parent_model_name = get_model_name(parent_factory_class) if parent_factory_class is not None else None

    def is_dep(value: Any) -> bool:
        if isinstance(value, factory.RelatedFactory):
            return False
        if isinstance(value, factory.SubFactory) and get_model_name(value.get_factory()) == parent_model_name:
            return False
        if isinstance(value, factory.declarations.PostGenerationDeclaration):
            # Dependency on extracted value
            return True

        return True

    return [
        SEPARATOR.join((model_name, attr)) for attr, value in factory_class._meta.declarations.items() if is_dep(value)
    ]


def evaluate(request: SubRequest, value: LazyFixture | Any) -> Any:
    """Evaluate the declaration (lazy fixtures, etc)."""
    return value.evaluate(request) if isinstance(value, LazyFixture) else value


def model_fixture(request: SubRequest, factory_name: str) -> Any:
    """Model fixture implementation."""
    factoryboy_request: FactoryboyRequest = request.getfixturevalue("factoryboy_request")

    # Try to evaluate as much post-generation dependencies as possible
    factoryboy_request.evaluate(request)

    assert request.fixturename  # NOTE: satisfy mypy
    fixture_name = request.fixturename
    prefix = "".join((fixture_name, SEPARATOR))

    factory_class: FactoryType = request.getfixturevalue(factory_name)

    # Create model fixture instance
    Factory: FactoryType = cast(FactoryType, type("Factory", (factory_class,), {}))
    # equivalent to:
    # class Factory(factory_class):
    #     pass
    # it just makes mypy understand it.

    Factory._meta.base_declarations = {
        k: v
        for k, v in Factory._meta.base_declarations.items()
        if not isinstance(v, factory.declarations.PostGenerationDeclaration)
    }
    Factory._meta.post_declarations = factory.builder.DeclarationSet()

    kwargs = {}
    for key in factory_class._meta.pre_declarations:
        argname = "".join((prefix, key))
        if argname in request._fixturedef.argnames:
            kwargs[key] = evaluate(request, request.getfixturevalue(argname))

    strategy = factory.enums.CREATE_STRATEGY
    builder = factory.builder.StepBuilder(Factory._meta, kwargs, strategy)
    step = factory.builder.BuildStep(builder=builder, sequence=Factory._meta.next_sequence())

    instance = Factory(**kwargs)

    # Cache the instance value on pytest level so that the fixture can be resolved before the return
    request._fixturedef.cached_result = (instance, 0, None)
    request._fixture_defs[fixture_name] = request._fixturedef

    # Defer post-generation declarations
    deferred: list[DeferredFunction] = []

    for attr in factory_class._meta.post_declarations.sorted():

        decl = factory_class._meta.post_declarations.declarations[attr]

        if isinstance(decl, factory.RelatedFactory):
            deferred.append(make_deferred_related(factory_class, fixture_name, attr))
        else:
            argname = "".join((prefix, attr))
            extra = {}
            for k, v in factory_class._meta.post_declarations.contexts[attr].items():
                if k == "":
                    continue
                post_attr = SEPARATOR.join((argname, k))

                if post_attr in request._fixturedef.argnames:
                    extra[k] = evaluate(request, request.getfixturevalue(post_attr))
                else:
                    extra[k] = v
            # Handle special case for ``PostGenerationMethodCall`` where
            # `attr_fixture` value is equal to ``NotProvided``, which mean
            # that `value_provided` should be falsy
            postgen_value = evaluate(request, request.getfixturevalue(argname))
            postgen_context = PostGenerationContext(
                value_provided=(postgen_value is not NotProvided),
                value=postgen_value,
                extra=extra,
            )
            deferred.append(
                make_deferred_postgen(step, factory_class, fixture_name, instance, attr, decl, postgen_context)
            )
    factoryboy_request.defer(deferred)

    # Try to evaluate as much post-generation dependencies as possible
    factoryboy_request.evaluate(request)
    return instance


def make_deferred_related(factory: FactoryType, fixture: str, attr: str) -> DeferredFunction:
    """Make deferred function for the related factory declaration.

    :param factory: Factory class.
    :param fixture: Object fixture name e.g. "book".
    :param attr: Declaration attribute name e.g. "publications".

    :note: Deferred function name results in "book__publication".
    """
    name = SEPARATOR.join((fixture, attr))

    def deferred_impl(request: SubRequest) -> Any:
        return request.getfixturevalue(name)

    return DeferredFunction(
        name=name,
        factory=factory,
        is_related=True,
        function=deferred_impl,
    )


def make_deferred_postgen(
    step: BuildStep,
    factory_class: FactoryType,
    fixture: str,
    instance: Any,
    attr: str,
    declaration: PostGeneration,
    context: PostGenerationContext,
) -> DeferredFunction:
    """Make deferred function for the post-generation declaration.

    :param step: factory_boy builder step.
    :param factory_class: Factory class.
    :param fixture: Object fixture name e.g. "author".
    :param instance: Parent object instance.
    :param attr: Declaration attribute name e.g. "register_user".
    :param context: Post-generation declaration context.

    :note: Deferred function name results in "author__register_user".
    """
    name = SEPARATOR.join((fixture, attr))

    def deferred_impl(request: SubRequest) -> Any:
        return declaration.call(instance, step, context)

    return DeferredFunction(
        name=name,
        factory=factory_class,
        is_related=False,
        function=deferred_impl,
    )


def factory_fixture(request: SubRequest, factory_class: F) -> F:
    """Factory fixture implementation."""
    return factory_class


def attr_fixture(request: SubRequest, value: T) -> T:
    """Attribute fixture implementation."""
    return value


def subfactory_fixture(request: SubRequest, factory_class: FactoryType) -> Any:
    """SubFactory/RelatedFactory fixture implementation."""
    fixture = inflection.underscore(factory_class._meta.model.__name__)
    return request.getfixturevalue(fixture)


def get_caller_locals(depth: int = 2) -> dict[str, Any]:
    """Get the local namespace of the caller frame."""
    return sys._getframe(depth).f_locals


class LazyFixture:
    """Lazy fixture."""

    def __init__(self, fixture: FixtureFunction | str) -> None:
        """Lazy pytest fixture wrapper.

        :param fixture: Fixture name or callable with dependencies.
        """
        self.fixture = fixture
        if callable(self.fixture):
            params = signature(self.fixture).parameters.values()
            self.args = [param.name for param in params if param.kind == param.POSITIONAL_OR_KEYWORD]
        else:
            self.args = [self.fixture]

    def evaluate(self, request: SubRequest) -> Any:
        """Evaluate the lazy fixture.

        :param request: pytest request object.
        :return: evaluated fixture.
        """
        if callable(self.fixture):
            kwargs = {arg: request.getfixturevalue(arg) for arg in self.args}
            return self.fixture(**kwargs)
        else:
            return request.getfixturevalue(self.fixture)

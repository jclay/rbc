from . import register_external
from numba.core import imputils, typing  # noqa: E402
from numba.cuda import libdevicefuncs  # noqa: E402

# Typing
typing_registry = typing.templates.Registry()

# Lowering
lowering_registry = imputils.Registry()

for fname, (retty, args) in libdevicefuncs.functions.items():
    argtys = tuple(map(lambda x: f"{x.ty}*" if x.is_ptr else f"{x.ty}", args))
    register_external(
        fname, retty, argtys, __name__, globals(), typing_registry, lowering_registry)

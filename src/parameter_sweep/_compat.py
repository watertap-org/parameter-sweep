"""
Internal utility module for utility functions needed for compatibility, none of which should be needed long-term.
"""

try:
    from watertap.core.solvers import get_solver
except ModuleNotFoundError:
    # either watertap is not installed, or is a version prior to watertap-org/watertap#1353
    # in either case, we use get_solver() from IDAES
    from idaes.core.solvers import get_solver

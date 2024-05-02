"""
Internal utility module for utility functions needed for compatibility, none of which should be needed long-term.
"""

try:
    from watertap.core.solvers import get_solver
except ModuleNotFoundError:
    # prior to watertap-org/watertap#1353, using get_solver() from IDAES
    from idaes.core.solvers import get_solver

    # and then importing watertap to override the IDAES solver with the WaterTAP settings
    import watertap

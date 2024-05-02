#################################################################################
# WaterTAP Copyright (c) 2020-2024, The Regents of the University of California,
# through Lawrence Berkeley National Laboratory, Oak Ridge National Laboratory,
# National Renewable Energy Laboratory, and National Energy Technology
# Laboratory (subject to receipt of any required approvals from the U.S. Dept.
# of Energy). All rights reserved.
#
# Please see the files COPYRIGHT.md and LICENSE.md for full copyright and license
# information, respectively. These files are also available online at the URL
# "https://github.com/watertap-org/watertap/"
#################################################################################
import numpy as np
import pyomo.environ as pyo
import warnings
import copy
import time

from abc import abstractmethod, ABC

from idaes.core.surrogate.pysmo import sampling
from pyomo.common.deprecation import deprecation_warning
from pyomo.common.config import ConfigValue
from pyomo.common.modeling import unique_component_name
from pyomo.core.base import _VarData, _ExpressionData
from pyomo.core.base.param import _ParamData
from pyomo.common.dependencies import attempt_import

requests, requests_available = attempt_import("requests")

from parameter_sweep.writer import ParameterSweepWriter
from parameter_sweep.sampling_types import (
    SamplingType,
    LinearSample,
    SetMode,
)

from parameter_sweep.parallel.parallel_manager_factory import create_parallel_manager

from parameter_sweep.model_manager import ModelManager
from parameter_sweep.parallel_utils import (
    _ParameterSweepParallelUtils,
    return_none,
)
from parameter_sweep._compat import get_solver


def _default_optimize(model, options=None, tee=False):
    """
    Default optimization function used in parameter_sweep.
    Optimizes ``model`` using the IDAES default solver.
    Raises a RuntimeError if the TerminationCondition is not optimal

    Arguments:

        model : A Pyomo ConcreteModel to optimize

        options (optional) : Solver options to pass into idaes.core.utils.get_solver.
                             Default is None
        tee (options) : To display the solver log. Default it False

    """
    solver = get_solver(options=options)
    results = solver.solve(model, tee=tee)
    return results


class _ParameterSweepBase(ABC):
    CONFIG = ParameterSweepWriter.CONFIG()

    CONFIG.declare(
        "build_model",
        ConfigValue(
            default=None,
            # domain=function,
            description="Function for building the model.",
        ),
    )

    CONFIG.declare(
        "build_model_kwargs",
        ConfigValue(
            default=dict(),
            domain=dict,
            description="Keyword argument for the model build function for the parameter sweep.",
        ),
    )
    CONFIG.declare(
        "build_sweep_params",
        ConfigValue(
            default=None,
            # domain=function,
            description="Function for building the sweep_paramters",
        ),
    )
    CONFIG.declare(
        "build_sweep_params_kwargs",
        ConfigValue(
            default=dict(),
            domain=dict,
            description="Keyword argument for the build sweep params function for the parameter sweep.",
        ),
    )

    CONFIG.declare(
        "build_outputs",
        ConfigValue(
            default=None,
            # domain=function,
            description="Function for building outputs",
        ),
    )
    CONFIG.declare(
        "build_outputs_kwargs",
        ConfigValue(
            default=dict(),
            domain=dict,
            description="Keyword argument for the build outputs function for the parameter sweep.",
        ),
    )

    CONFIG.declare(
        "optimize_function",
        ConfigValue(
            default=_default_optimize,
            # domain=function,
            description="Optimization function to be used for the parameter sweep.",
        ),
    )

    CONFIG.declare(
        "optimize_kwargs",
        ConfigValue(
            default=dict(),
            domain=dict,
            description="Keyword argument for the optimization function for the parameter sweep.",
        ),
    )
    CONFIG.declare(
        "initialize_function",
        ConfigValue(
            default=None,
            # domain=function,
            description="Function to reinitialize a flowsheet",
        ),
    )
    CONFIG.declare(
        "update_sweep_params_before_init",
        ConfigValue(
            default=False,
            # domain=function,
            description="Enables update of vars to sweep values before initilization (only enabled if init_before_sweep=True)",
        ),
    )
    CONFIG.declare(
        "initialize_kwargs",
        ConfigValue(
            default=dict(),
            domain=dict,
            description="Keyword arguments for the initialization function.",
        ),
    )

    CONFIG.declare(
        "initialize_before_sweep",
        ConfigValue(
            default=False,
            domain=bool,
            description="Initializing a model before every iteration.",
        ),
    )

    CONFIG.declare(
        "reinitialize_function",
        ConfigValue(
            default=None,
            # domain=function,
            description="Function to reinitialize a flowsheet",
        ),
    )
    CONFIG.declare(
        "reinitialize_kwargs",
        ConfigValue(
            default=dict(),
            domain=dict,
            description="Keyword arguments for the reinitialization function.",
        ),
    )

    CONFIG.declare(
        "reinitialize_before_sweep",
        ConfigValue(
            default=False,
            domain=bool,
            description="Reinitializing a model before every iteration.",
        ),
    )

    CONFIG.declare(
        "probe_function",
        ConfigValue(
            default=None,
            description="Function to probe if a flowsheet configuration will work",
        ),
    )

    CONFIG.declare(
        "custom_do_param_sweep",
        ConfigValue(
            default=None,
            description="Alternative implementation of the parameter sweep function in case the user is doing unique analyses.",
        ),
    )

    CONFIG.declare(
        "custom_do_param_sweep_kwargs",
        ConfigValue(
            default=dict(),
            domain=dict,
            description="Alternative implementation of the parameter sweep function in case the user is doing unique analyses.",
        ),
    )

    CONFIG.declare(
        "publish_progress",
        ConfigValue(
            default=False,
            domain=bool,
            description="Boolean to decide whether information about how many iterations of the parameter sweep have completed should be sent.",
        ),
    )

    CONFIG.declare(
        "publish_address",
        ConfigValue(
            default="http://localhost:8888",
            domain=str,
            description="Address to which the parameter sweep progress will be sent.",
        ),
    )

    CONFIG.declare(
        "number_of_subprocesses",
        ConfigValue(
            default=1,
            domain=int,
            description="Number of processes to fan out to locally - ignored if running under MPI.",
        ),
    )
    CONFIG.declare(
        "parallel_back_end",
        ConfigValue(
            default="ConcurrentFutures",
            domain=str,
            description="Backend for parallelization, if not useing MPI",
        ),
    )
    CONFIG.declare(
        "log_model_states",
        ConfigValue(
            default=False,
            domain=bool,
            description="Enables loging of model states during serial execution",
        ),
    )
    CONFIG.declare(
        "index_global_combo_array",
        ConfigValue(
            default=False,
            domain=bool,
            description="Will add indexing to global_combo_array, primarily used with differential parameter sweep tool",
        ),
    )

    def __init__(
        self,
        **options,
    ):
        parallel_manager_class = options.pop("parallel_manager_class", None)
        self.model = None

        self.model_manager = None
        self.config = self.CONFIG(options)
        self.parallel_manager = create_parallel_manager(
            parallel_manager_class=parallel_manager_class,
            number_of_subprocesses=self.config.number_of_subprocesses,
            parallel_back_end=self.config.parallel_back_end,
        )

        # Initialize the writer
        self.writer = ParameterSweepWriter(
            self.parallel_manager,
            csv_results_file_name=self.config.csv_results_file_name,
            h5_results_file_name=self.config.h5_results_file_name,
            debugging_data_dir=self.config.debugging_data_dir,
            interpolate_nan_outputs=self.config.interpolate_nan_outputs,
            h5_parent_group_name=self.config.h5_parent_group_name,
        )

    @staticmethod
    def assign_variable_names(model, outputs):
        # Only assign output variable names to unassigned outputs
        exprs = pyo.Expression(pyo.Any)
        model.add_component(
            unique_component_name(model, "_parameter_sweep_expressions"), exprs
        )
        for output_name, _pyo_obj in outputs.items():
            if not isinstance(_pyo_obj, (_VarData, _ExpressionData, _ParamData)):
                # Add this object as an expression and assign a name
                exprs[output_name] = _pyo_obj
                outputs[output_name] = exprs[output_name]

    def _publish_updates(self, iteration, solve_status, solve_time):
        if not requests_available:
            raise ImportError(
                "requests (parameter_sweep optional dependency) not installed"
            )

        if self.config.publish_progress:
            publish_dict = {
                "worker_number": self.parallel_manager.get_rank(),
                "iteration": iteration,
                "solve_status": solve_status,
                "solve_time": solve_time,
            }

            return requests.put(self.config.publish_address, data=publish_dict)

    def _create_global_combo_array(self, d, sampling_type):
        num_var_params = len(d)
        param_values = []
        # if users provides many parameters to sweep over, it will create a large
        # multi dimensional array when sampling type is FIXED,
        # so here we track single value samples and multi value sample,
        # we meshgrid only those that have more then one value, and then
        # merge all samples into a single 2D array for running PS sweep.
        mix_mesh_idx, mix_mesh_samples = (
            [],
            [],
        )  # tracking params with multiple sweep values
        single_idx, single_samples = (
            [],
            [],
        )  # tracking params with only a single sweep values
        sample_i = 0  # keeps track of sample order
        for k, v in d.items():
            # Build a vector of discrete values for this parameter
            p = v.sample()
            param_values.append(p)
            if len(p) == 1:
                single_samples.append(p[0])
                single_idx.append(sample_i)
            else:
                mix_mesh_samples.append(p)
                mix_mesh_idx.append(sample_i)
            sample_i += 1
        if sampling_type == SamplingType.FIXED:
            # Form an array with every possible combination of parameter values if
            # we have any samples with more then one sweep value
            if len(mix_mesh_idx) > 0:
                temp_global_combo_array = np.array(
                    np.meshgrid(*mix_mesh_samples, indexing="ij")
                )
                temp_global_combo_array = temp_global_combo_array.reshape(
                    len(mix_mesh_samples), -1
                ).T
                global_combo_array = np.zeros(
                    (temp_global_combo_array.shape[0], len(param_values))
                )
            else:
                global_combo_array = np.zeros((1, len(param_values)))

            # populate array with sweep params
            for i, g_i in enumerate(single_idx):
                global_combo_array[:, g_i] = single_samples[i]

            for i, g_i in enumerate(mix_mesh_idx):
                global_combo_array[:, g_i] = temp_global_combo_array[:, i]

        elif sampling_type == SamplingType.RANDOM:
            sorting = np.argsort(param_values[0])
            global_combo_array = np.vstack(param_values).T
            global_combo_array = global_combo_array[sorting, :]

        elif sampling_type == SamplingType.RANDOM_LHS:
            lb = [val[0] for val in param_values]
            ub = [val[1] for val in param_values]
            lhs = sampling.LatinHypercubeSampling(
                [lb, ub], number_of_samples=v.num_samples, sampling_type="creation"
            )
            global_combo_array = lhs.sample_points()
            sorting = np.argsort(global_combo_array[:, 0])
            global_combo_array = global_combo_array[sorting, :]

        else:
            raise ValueError(f"Unknown sampling type: {sampling_type}")

        # Test if the global_combo_array is in row-major order
        if not global_combo_array.flags.c_contiguous:
            # If not, return a copy of this array with row-major memory order
            global_combo_array = np.ascontiguousarray(global_combo_array)
        # add sample index for tracking of sample number in parallel schema
        if self.config.index_global_combo_array:
            sample_idx = np.arange(global_combo_array.shape[0]).reshape(-1, 1)
            global_combo_array = np.hstack((sample_idx, global_combo_array))
        return global_combo_array

    """
    Put together all of the parameter combinations that the sweep will be run for.
    """

    def _build_combinations(self, d, sampling_type, num_samples):
        # only build the full array of combinations on the root process. on the non-root
        # processes, initialize an empty array of the right size that will be synced
        # over from the root process.

        global_combo_array = self._create_global_combo_array(d, sampling_type)
        self.parallel_manager.sync_array_with_peers(global_combo_array)

        return global_combo_array

    def _divide_combinations(self, global_combo_array):
        # Split the total list of combinations into NUM_PROCS chunks,
        # one per each of the MPI ranks
        # divided_combo_array = np.array_split(global_combo_array, num_procs, axis=0)
        divided_combo_array = np.array_split(
            global_combo_array, self.parallel_manager.number_of_worker_processes()
        )

        # Return only this rank's portion of the total workload
        local_combo_array = divided_combo_array[self.parallel_manager.get_rank()]

        return local_combo_array

    def _get_object(self, model, pyomo_object):
        name = pyomo_object.name

        # seems to be a bug, as indexed var with [None] exists
        # but can't be found by find_component
        if "[None]" in name:
            name = name.replace("[None]", "")
            return model.find_component(name)[None]
        else:
            return model.find_component(name)

    def _update_model_values(self, m, param_dict, values):
        # remove index from values
        if self.config.index_global_combo_array:
            non_indexed_values = values[1:]
        else:
            non_indexed_values = values

        for k, item in enumerate(param_dict.values()):
            param = self._get_object(m, item.pyomo_object)
            if param.is_variable_type():
                # Fix the single value to values[k]
                if item.set_mode == SetMode.FIX_VALUE:
                    param.fix(non_indexed_values[k])
                elif item.set_mode == SetMode.SET_LB:
                    param.setlb(non_indexed_values[k])
                elif item.set_mode == SetMode.SET_UB:
                    param.setub(non_indexed_values[k])
                # In SET_FIXED_STATE  we are only fixing or unfixing values
                elif item.set_mode == SetMode.SET_FIXED_STATE:
                    if item.default_fixed_value is not None:
                        param.fix(item.default_fixed_value)
                    if non_indexed_values[k]:
                        param.fix()
                    else:
                        param.unfix()
            elif param.is_parameter_type():
                # Fix the single value to values[k]
                param.set_value(non_indexed_values[k])

            else:
                raise RuntimeError(f"Unrecognized Pyomo object {param}")

    def _aggregate_results_arr(self, global_results_dict, num_cases):
        global_results = np.zeros(
            (num_cases, len(global_results_dict["outputs"])), dtype=float
        )

        if self.parallel_manager.is_root_process():
            for i, (key, item) in enumerate(global_results_dict["outputs"].items()):
                global_results[:, i] = item["value"][:num_cases]

        self.parallel_manager.sync_array_with_peers(global_results)

        return global_results

    def _process_sweep_params(self, sweep_params):
        sampling_type = None

        # Check the list of parameters to make sure they are valid
        for k in sweep_params.keys():
            # Convert to using Sample class
            if isinstance(sweep_params[k], (list, tuple)):
                sweep_params[k] = LinearSample(*sweep_params[k])

            # Get the type of sampling
            current_sampling_type = sweep_params[k].sampling_type

            # Check to make sure only one sampling type is provided
            if sampling_type is None:
                sampling_type = current_sampling_type
            elif current_sampling_type != sampling_type:
                raise ValueError("Cannot mix sampling types")

        return sweep_params, sampling_type

    def _create_local_output_skeleton(self, model, sweep_params, outputs, num_samples):
        output_dict = {}
        output_dict["sweep_params"] = {}
        output_dict["outputs"] = {}

        # Store the inputs
        for param_name, sampling_obj in sweep_params.items():
            var = sampling_obj.pyomo_object
            output_dict["sweep_params"][param_name] = (
                self._create_component_output_skeleton(var, num_samples)
            )

        if outputs is None:
            # No outputs are specified, so every Var, Expression, and Objective on the model should be saved
            for pyo_obj in model.component_data_objects(
                (pyo.Var, pyo.Expression, pyo.Objective, pyo.Param), active=True
            ):
                # We do however need to make sure that the short name for the inputs is used here
                for param_name, sampling_obj in sweep_params.items():
                    if pyo_obj.name == sampling_obj.pyomo_object.name:
                        output_dict["outputs"][param_name] = (
                            self._create_component_output_skeleton(pyo_obj, num_samples)
                        )
                    else:
                        output_dict["outputs"][pyo_obj.name] = (
                            self._create_component_output_skeleton(pyo_obj, num_samples)
                        )

        else:
            # Save only the outputs specified in the outputs dictionary
            for short_name, pyo_obj in outputs.items():
                output_dict["outputs"][short_name] = (
                    self._create_component_output_skeleton(
                        self._get_object(model, pyo_obj), num_samples
                    )
                )
        return output_dict

    def _create_component_output_skeleton(self, component, num_samples):
        comp_dict = {}
        comp_dict["value"] = np.zeros(num_samples, dtype=float)

        if hasattr(component, "lb"):
            comp_dict["lower bound"] = component.lb
        if hasattr(component, "ub"):
            comp_dict["upper bound"] = component.ub
        if hasattr(component, "get_units"):
            unit_obj = component.get_units()
            if unit_obj is not None:
                comp_dict["units"] = component.get_units().name
            else:
                comp_dict["units"] = "None"

        # Add information to this output that WILL NOT be written as part
        # of the file saving step.
        comp_dict["full_name"] = component.name

        return comp_dict

    def _update_local_output_dict(
        self, model, sweep_params, case_number, run_successful, output_dict
    ):
        # Get the inputs
        op_ps_dict = output_dict["sweep_params"]
        for key, item in sweep_params.items():
            # stores value actually applied to model, rather one assumed to be applied
            op_ps_dict[key]["value"][case_number] = self._get_object(
                model, item.pyomo_object
            ).value

        # Get the outputs from model
        if run_successful:
            for var_name, specs in output_dict["outputs"].items():
                pyo_obj = model.find_component(specs["full_name"])
                # incase value is not initlized or can't be evalauted
                # typical case, is a var is created, but not initlized or touched, such is 0 index vars in 1D RO
                try:
                    output_dict["outputs"][var_name]["value"][case_number] = pyo.value(
                        pyo_obj
                    )
                except ValueError:
                    pass

        else:
            for label, specs in output_dict["outputs"].items():
                pyo_obj = model.find_component(specs["full_name"])
                if pyo_obj.name in sweep_params.keys():
                    output_dict["outputs"][label]["value"][case_number] = pyo.value(
                        pyo_obj
                    )
                else:
                    output_dict["outputs"][label]["value"][case_number] = np.nan

    def _create_global_output(self, local_output_dict, req_num_samples=None):
        # We make the assumption that the parameter sweep is running the same
        # flowsheet num_samples number of times, i.e., the structure of the
        # local_output_dict remains the same across all mpi_ranks
        local_num_cases = len(local_output_dict["solve_successful"])

        # Gather the size of the value array for each peer process
        sample_split_arr = self.parallel_manager.combine_data_with_peers(
            local_num_cases
        )

        num_total_samples = sum(sample_split_arr)
        if req_num_samples is None:
            req_num_samples = num_total_samples

        # Create the global value array on rank 0
        if self.parallel_manager.is_root_process():
            global_output_dict = copy.deepcopy(local_output_dict)
            # Create a global value array of inputs in the dictionary
            for key, item in global_output_dict.items():
                if key in ["sweep_params", "outputs"]:
                    for subkey, subitem in item.items():
                        subitem["value"] = np.zeros(num_total_samples, dtype=float)

        else:
            global_output_dict = local_output_dict

        # Finally collect the values
        for key, item in local_output_dict.items():
            if key in ["sweep_params", "outputs"]:
                for subkey, subitem in item.items():
                    self.parallel_manager.gather_arrays_to_root(
                        sendbuf=subitem["value"],
                        recvbuf_spec=(
                            global_output_dict[key][subkey]["value"],
                            sample_split_arr,
                        ),
                    )

                    # Trim to the exact number
                    global_output_dict[key][subkey]["value"] = global_output_dict[key][
                        subkey
                    ]["value"][0:req_num_samples]

            elif key == "solve_successful":
                local_solve_successful = np.fromiter(item, dtype=bool, count=len(item))

                if self.parallel_manager.is_root_process():
                    global_solve_successful = np.empty(num_total_samples, dtype=bool)
                else:
                    global_solve_successful = None

                self.parallel_manager.gather_arrays_to_root(
                    sendbuf=local_solve_successful,
                    recvbuf_spec=(global_solve_successful, sample_split_arr),
                )

                if self.parallel_manager.is_root_process():
                    # Trim to the exact number
                    global_output_dict[key] = list(
                        global_solve_successful[0:req_num_samples]
                    )

        return global_output_dict

    def _param_sweep_kernel(self, sweep_params, local_value_k):
        # Forced reinitialization of the flowsheet if enabled
        # and model is not already initalized at givel local sweep param set
        # or init if model was not initialized or prior solved failed (if solved failed, init state is false)
        if (
            self.config.initialize_before_sweep
            and all(self.model_manager.current_k == local_value_k) == False
        ) or self.model_manager.is_initialized == False:
            if self.model_manager._is_rebuild_and_init_enabled:
                self.model_manager.build_and_init(sweep_params, local_value_k)
        # try to solve our model
        self.model_manager.update_model_params(sweep_params, local_value_k)
        self.model_manager.solve_model()

        # if model failed to solve from a prior paramter solved state, lets try
        # to re-init and solve again
        if (
            self.model_manager.is_solved == False
            and self.model_manager.is_prior_parameter_solved == True
            and self.model_manager._is_rebuild_and_init_enabled
        ):
            self.model_manager.build_and_init(sweep_params, local_value_k)
            self.model_manager.update_model_params(sweep_params, local_value_k)
            self.model_manager.solve_model()
        # return model solved state
        return self.model_manager.is_solved

    def _run_sample(
        self,
        local_value_k,
        k,
        sweep_params,
        local_output_dict,
    ):
        # Update model parmeters for record keeping and probe testing
        self._update_model_values(self.model_manager.model, sweep_params, local_value_k)

        if self.config.probe_function is None or self.config.probe_function(
            self.model_manager.model
        ):
            run_successful = self._param_sweep_kernel(
                sweep_params,
                local_value_k,
            )
        else:
            run_successful = False
            # makes sure that if model was build,, but failed to init
            # we store the pars that were run

        # Update the loop based on the reinitialization
        self._update_local_output_dict(
            self.model_manager.model,
            sweep_params,
            k,
            run_successful,
            local_output_dict,
        )
        return run_successful

    def _do_param_sweep(self, sweep_params, outputs, local_values):
        # setup model manager if not already specifid (Used in case of diff tool)
        # or if user wants to specify thier own model_manager before runing param sweep
        if self.model_manager == None:
            self.model_manager = ModelManager(self)

        # build and init model, we also pass first set of paramters incase user wants
        # to update them before initlizeing the model
        if (
            self.config.initialize_before_sweep
            or self.model_manager.is_initialized == False
        ):
            if self.model_manager._is_rebuild_and_init_enabled:
                self.model_manager.build_and_init(
                    sweep_params=sweep_params, local_value_k=local_values[0, :]
                )

        local_num_cases = np.shape(local_values)[0]

        # Create the output skeleton for storing detailed data
        local_output_dict = self._create_local_output_skeleton(
            self.model_manager.model, sweep_params, outputs, local_num_cases
        )

        local_solve_successful_list = []

        # ================================================================
        # Run all optimization cases
        # ================================================================

        for k in range(local_num_cases):
            start_time = time.time()
            run_successful = self._run_sample(
                local_values[k, :],
                k,
                sweep_params,
                local_output_dict,
            )
            time_elapsed = time.time() - start_time
            local_solve_successful_list.append(run_successful)
            self._publish_updates(k, run_successful, time_elapsed)

        local_output_dict["solve_successful"] = local_solve_successful_list

        return local_output_dict

    @abstractmethod
    def parameter_sweep(self, *args, **kwargs):
        pass


class ParameterSweep(_ParameterSweepBase, _ParameterSweepParallelUtils):
    CONFIG = _ParameterSweepBase.CONFIG()

    def parameter_sweep(
        self,
        build_model,
        build_sweep_params,
        build_outputs=None,
        build_outputs_kwargs=None,
        num_samples=None,
        seed=None,
        build_model_kwargs=None,
        build_sweep_params_kwargs=None,
    ):
        build_model_kwargs = (
            build_model_kwargs if build_model_kwargs is not None else dict()
        )
        build_outputs_kwargs = (
            build_outputs_kwargs if build_outputs_kwargs is not None else dict()
        )
        build_sweep_params_kwargs = (
            build_sweep_params_kwargs
            if build_sweep_params_kwargs is not None
            else dict()
        )

        if not callable(build_model):
            _model = build_model
            build_model = lambda: _model
            deprecation_warning(
                "Passing a model directly to the parameter_sweep function is deprecated \
                                and will not work with future implementations of parallelism.",
                version="0.10.0",
            )

        if not callable(build_sweep_params):
            _sweep_params = build_sweep_params
            build_sweep_params = lambda model: _sweep_params
            deprecation_warning(
                "Passing sweep params directly to the parameter_sweep function is deprecated \
                                and will not work with future implementations of parallelism.",
                version="0.10.0",
            )

        if build_outputs is None:
            build_outputs = return_none

        if not callable(build_outputs):
            _combined_outputs = build_outputs
            build_outputs = lambda model: _combined_outputs
            deprecation_warning(
                "Passing the output dict directly to the parameter_sweep function is deprecated \
                                and will not work with future implementations of parallelism.",
                version="0.10.0",
            )
        # This should be depreciated in future versions
        self.config.build_model = build_model
        self.config.build_sweep_params = build_sweep_params
        self.config.build_outputs = build_outputs
        self.config.build_outputs_kwargs = build_outputs_kwargs
        self.config.build_model_kwargs = build_model_kwargs
        self.config.build_sweep_params_kwargs = build_sweep_params_kwargs
        # create the list of all combinations - needed for some aspects of scattering
        model = build_model(**build_model_kwargs)
        sweep_params = build_sweep_params(model, **build_sweep_params_kwargs)
        sweep_params, sampling_type = self._process_sweep_params(sweep_params)
        np.random.seed(seed)
        all_parameter_combinations = self._build_combinations(
            sweep_params, sampling_type, num_samples
        )

        all_results = self.run_scatter_gather(
            all_parameter_combinations, ParameterSweep
        )

        global_sweep_results_dict = self._combine_gather_results(all_results)
        combined_output_arr = self._combine_output_array(global_sweep_results_dict)
        all_parameter_combinations_solved = self._combine_input_array(
            global_sweep_results_dict
        )
        # save the results for all simulations run by this process and its children
        for results in self.parallel_manager.results_from_local_tree(all_results):
            self.writer.save_results(
                sweep_params,
                results.parameters,
                all_parameter_combinations_solved,
                results.results,
                global_sweep_results_dict,
                combined_output_arr,
                process_number=results.process_number,
            )

        global_save_data = np.hstack((all_parameter_combinations, combined_output_arr))

        return global_save_data, global_sweep_results_dict


class RecursiveParameterSweep(_ParameterSweepBase):
    CONFIG = _ParameterSweepBase.CONFIG()

    def _filter_recursive_solves(
        self, model, sweep_params, outputs, recursive_local_dict
    ):
        # Figure out how many filtered solves did this rank actually do
        filter_counter = 0
        for case, content in recursive_local_dict.items():
            filter_counter += sum(content["solve_successful"])

        # Now that we have all of the local output dictionaries, we need to construct
        # a consolidated dictionary of successful solves.
        local_filtered_dict = self._create_local_output_skeleton(
            model, sweep_params, outputs, filter_counter
        )
        local_filtered_dict["solve_successful"] = []

        # Populate local_successful_outputs
        offset = 0
        for case_number, content in recursive_local_dict.items():
            # Filter all of the sucessful solves
            optimal_indices = [
                idx
                for idx, success in enumerate(content["solve_successful"])
                if success
            ]
            n_successful_solves = len(optimal_indices)
            stop = offset + n_successful_solves

            for key, item in content.items():
                if key != "solve_successful":
                    for subkey, subitem in item.items():
                        local_filtered_dict[key][subkey]["value"][offset:stop] = (
                            subitem["value"][optimal_indices]
                        )

            # Place the solve status
            local_filtered_dict["solve_successful"].extend(
                [content["solve_successful"][i] for i in optimal_indices]
            )

            offset += n_successful_solves

        return local_filtered_dict, filter_counter

    def _aggregate_filtered_input_arr(self, global_filtered_dict, req_num_samples):
        global_filtered_values = np.zeros(
            (req_num_samples, len(global_filtered_dict["sweep_params"])),
            dtype=float,
        )

        if self.parallel_manager.is_root_process():
            for i, (key, item) in enumerate(
                global_filtered_dict["sweep_params"].items()
            ):
                global_filtered_values[:, i] = item["value"][:req_num_samples]

        self.parallel_manager.sync_array_with_peers(global_filtered_values)

        return global_filtered_values

    def _aggregate_filtered_results(self, local_filtered_dict, req_num_samples):
        global_filtered_dict = self._create_global_output(
            local_filtered_dict, req_num_samples
        )
        global_filtered_results = self._aggregate_results_arr(
            global_filtered_dict, req_num_samples
        )
        global_filtered_values = self._aggregate_filtered_input_arr(
            global_filtered_dict, req_num_samples
        )

        return global_filtered_dict, global_filtered_results, global_filtered_values

    @staticmethod
    def _update_sweep_params(sweep_params, num_total_samples):
        for obj in sweep_params.values():
            obj.num_samples = num_total_samples

    def parameter_sweep(
        self,
        build_model,
        build_sweep_params,
        build_outputs=None,
        build_outputs_kwargs=None,
        num_samples=None,
        seed=None,
        build_model_kwargs=None,
        build_sweep_params_kwargs=None,
    ):
        build_model_kwargs = (
            build_model_kwargs if build_model_kwargs is not None else dict()
        )
        build_outputs_kwargs = (
            build_outputs_kwargs if build_outputs_kwargs is not None else dict()
        )
        build_sweep_params_kwargs = (
            build_sweep_params_kwargs
            if build_sweep_params_kwargs is not None
            else dict()
        )

        if not callable(build_model):
            _model = build_model
            build_model = lambda: _model
            deprecation_warning(
                "Passing a model directly to the parameter_sweep function is deprecated \
                                and will not work with future implementations of parallelism.",
                version="0.10.0",
            )

        if not callable(build_sweep_params):
            _sweep_params = build_sweep_params
            build_sweep_params = lambda model: _sweep_params
            deprecation_warning(
                "Passing sweep params directly to the parameter_sweep function is deprecated \
                                and will not work with future implementations of parallelism.",
                version="0.10.0",
            )

        if build_outputs is None:
            build_outputs = return_none

        if not callable(build_outputs):
            _combined_outputs = build_outputs
            build_outputs = lambda model: _combined_outputs
            deprecation_warning(
                "Passing the output dict directly to the parameter_sweep function is deprecated \
                                and will not work with future implementations of parallelism.",
                version="0.10.0",
            )
        # This should be depreciated in future versions
        self.config.build_model = build_model
        self.config.build_sweep_params = build_sweep_params
        self.config.build_outputs = build_outputs
        self.config.build_outputs_kwargs = build_outputs_kwargs
        self.config.build_model_kwargs = build_model_kwargs
        self.config.build_sweep_params_kwargs = build_sweep_params_kwargs
        # create the list of all combinations - needed for some aspects of scattering
        model = build_model(**build_model_kwargs)
        sweep_params = build_sweep_params(model, **build_sweep_params_kwargs)
        sweep_params, sampling_type = self._process_sweep_params(sweep_params)
        outputs = build_outputs(model, **build_outputs_kwargs)
        # Set the seed before sampling
        np.random.seed(seed)

        # Check if the outputs have the name attribute. If not, assign one.
        if outputs is not None:
            self.assign_variable_names(model, outputs)

        n_samples_remaining = num_samples
        num_total_samples = num_samples

        local_output_collection = {}
        for loop_ctr in range(10):
            if n_samples_remaining <= 0:
                break

            if loop_ctr > 0:
                # We need to rebuild the sweep_params since these are single use objects
                self._update_sweep_params(sweep_params, num_total_samples)

            # Enumerate/Sample the parameter space
            global_values = self._build_combinations(
                sweep_params, sampling_type, num_total_samples
            )

            # divide the workload between processors
            local_values = self._divide_combinations(global_values)
            local_num_cases = np.shape(local_values)[0]
            if loop_ctr == 0:
                true_local_num_cases = local_num_cases

            if self.config.custom_do_param_sweep is None:
                local_output_collection[loop_ctr] = self._do_param_sweep(
                    sweep_params,
                    outputs,
                    local_values,
                )
            else:
                local_output_collection[loop_ctr] = (
                    self.self.config.custom_do_param_sweep(
                        sweep_params,
                        outputs,
                        local_values,
                        **self.config.custom_do_param_sweep_kwargs,
                    )
                )

            # Get the number of successful solves on this proc (sum of boolean flags)
            success_count = sum(local_output_collection[loop_ctr]["solve_successful"])
            failure_count = local_num_cases - success_count

            # Get the global number of successful solves and update the number of remaining samples
            if (
                self.parallel_manager.number_of_worker_processes() > 1
            ):  # pragma: no cover
                global_success_count = np.zeros(1, dtype=int)
                global_failure_count = np.zeros(1, dtype=int)

                self.parallel_manager.sum_values_and_sync(
                    sendbuf=np.array(success_count, dtype=int),
                    recvbuf=global_success_count,
                )

                self.parallel_manager.sum_values_and_sync(
                    sendbuf=np.array(failure_count, dtype=int),
                    recvbuf=global_failure_count,
                )
            else:
                global_success_count = success_count
                global_failure_count = failure_count

            success_prob = global_success_count / (
                global_failure_count + global_success_count
            )

            if success_prob < 0.1:
                warnings.warn(
                    f"Success rate of solves = {100.0*success_prob}%, consider adjusting sweep limits."
                )

            n_samples_remaining -= global_success_count

            # The total number of samples to generate at the next iteration is a multiple of the total remaining samples
            scale_factor = 2.0 / max(success_prob, 0.10)
            num_total_samples = int(np.ceil(scale_factor * n_samples_remaining))

        # Now that we have all of the local output dictionaries, we need to construct
        # a consolidated dictionary based on a filter, e.g., optimal solves.
        local_filtered_dict, local_n_successful = self._filter_recursive_solves(
            model, sweep_params, outputs, local_output_collection
        )

        # if we are debugging
        if self.writer.config["debugging_data_dir"] is not None:
            local_filtered_values = np.zeros(
                (local_n_successful, len(local_filtered_dict["sweep_params"])),
                dtype=float,
            )
            for i, (key, item) in enumerate(
                local_filtered_dict["sweep_params"].items()
            ):
                local_filtered_values[:, i] = item["value"][:]
        else:
            local_filtered_values = None

        # Not that we have all of the successful outputs in a consolidated dictionary locally,
        # we can now construct a global dictionary of successful solves.
        (
            global_filtered_dict,
            global_filtered_results,
            global_filtered_values,
        ) = self._aggregate_filtered_results(local_filtered_dict, num_samples)

        # Now we can save this
        self.parallel_manager.sync_with_peers()

        # Save to file
        global_save_data = self.writer.save_results(
            sweep_params,
            local_filtered_values,
            global_filtered_values,
            local_filtered_dict,
            global_filtered_dict,
            global_filtered_results,
            self.parallel_manager.get_rank(),
        )

        return global_save_data, global_filtered_dict

#    Copyright 2015-2015 ARM Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""A library for asserting scheduler scenarios based on the
statistics aggregation framework"""

import trappy
import itertools
import math
from trappy.plotter.Utils import listify
from trappy.stats.Aggregator import MultiTriggerAggregator
from trappy.stats import SchedConf as sconf
from bart.common import Utils
import numpy as np

# pylint: disable=invalid-name
# pylint: disable=too-many-arguments
class SchedAssert(object):

    """The primary focus of this class is to assert and verify
    predefined scheduler scenarios. This does not compare parameters
    across runs"""

    def __init__(self, run, topology, execname=None, pid=None):
        """Args:
                run (trappy.Run): A single trappy.Run object
                    or a path that can be passed to trappy.Run
                topology(trappy.stats.Topology): The CPU topology
                execname(str, optional): Optional execname of the task
                     under consideration.
                PID(int): The PID of the task to be checked

            One of pid or execname is mandatory. If only execname
            is specified, The current implementation will fail if
            there are more than one processes with the same execname
        """

        run = Utils.init_run(run)

        if not execname and not pid:
            raise ValueError("Need to specify at least one of pid or execname")

        self.execname = execname
        self._run = run
        self._pid = self._validate_pid(pid)
        self._aggs = {}
        self._topology = topology
        self._triggers = sconf.sched_triggers(self._run, self._pid,
                                              trappy.sched.SchedSwitch)
        self.name = "{}-{}".format(self.execname, self._pid)

    def _validate_pid(self, pid):
        """Validate the passed pid argument"""

        if not pid:
            pids = sconf.get_pids_for_process(self._run,
                                              self.execname)

            if len(pids) != 1:
                raise RuntimeError(
                    "There should be exactly one PID {0} for {1}".format(
                        pids,
                        self.execname))

            return pids[0]

        elif self.execname:

            pids = sconf.get_pids_for_process(self._run,
                                              self.execname)
            if pid not in pids:
                raise RuntimeError(
                    "PID {0} not mapped to {1}".format(
                        pid,
                        self.execname))
        else:
            self.execname = sconf.get_task_name(self._run, pid)

        return pid

    def _aggregator(self, aggfunc):
        """
        Returns an aggregator corresponding to the
        aggfunc, the aggregators are memoized for performance

        Args:
            aggfunc (function(pandas.Series)): Function parameter that
            accepts a pandas.Series object and returns a vector/scalar result
        """

        if aggfunc not in self._aggs.keys():
            self._aggs[aggfunc] = MultiTriggerAggregator(self._triggers,
                                                         self._topology,
                                                         aggfunc)
        return self._aggs[aggfunc]

    def getResidency(self, level, node, window=None, percent=False):
        """
        Residency of the task is the amount of time it spends executing
        a particular node of a topological level. For example:

        clusters=[]
        big = [1,2]
        little = [0,3,4,5]

        topology = Topology(clusters=clusters)

        level="cluster"
        node = [1,2]

        Will return the residency of the task on the big cluster. If
        percent is specified it will be normalized to the total RUNTIME
        of the TASK

        Args:
            level (hashable): The level to which the node belongs
            node (list): The node for which residency needs to calculated
            window (tuple): A (start, end) tuple to limit the scope of the
                residency calculation.
            percent: If true the result is normalized to the total runtime
                of the task and returned as a percentage
        """

        # Get the index of the node in the level
        node_index = self._topology.get_index(level, node)

        agg = self._aggregator(sconf.residency_sum)
        level_result = agg.aggregate(level=level, window=window)

        node_value = level_result[node_index]

        if percent:
            total = agg.aggregate(level="all", window=window)[0]
            node_value = node_value * 100
            node_value = node_value / total

        return node_value

    def assertResidency(
            self,
            level,
            node,
            expected_value,
            operator,
            window=None,
            percent=False):
        """
        Args:
            level (hashable): The level to which the node belongs
            node (list): The node for which residency needs to assert
            expected_value (double): The expected value of the residency
            operator (function): A binary operator function that returns
                a boolean
            window (tuple): A (start, end) tuple to limit the scope of the
                residency calculation.
            percent: If true the result is normalized to the total runtime
                of the task and returned as a percentage
        """
        node_value = self.getResidency(level, node, window, percent)
        return operator(node_value, expected_value)

    def getStartTime(self):
        """
        Returns the first time the task ran
        (across all CPUs)
        """

        agg = self._aggregator(sconf.first_time)
        result = agg.aggregate(level="all", value=sconf.TASK_RUNNING)
        return min(result[0])

    def getEndTime(self):
        """
        Returns the last time the task ran
        (across all CPUs)
        """

        agg = self._aggregator(sconf.first_time)
        agg = self._aggregator(sconf.last_time)
        result = agg.aggregate(level="all", value=sconf.TASK_RUNNING)
        return max(result[0])

    def _relax_switch_window(self, series, direction, window):
        """
            direction == "left"
                return the last time the task was running
                if no such time exists in the window,
                extend the window's left extent to
                getStartTime

            direction == "right"
                return the first time the task was running
                in the window. If no such time exists in the
                window, extend the window's right extent to
                getEndTime()

            The function returns a None if
            len(series[series == TASK_RUNNING]) == 0
            even in the extended window
        """

        series = series[series == sconf.TASK_RUNNING]
        w_series = sconf.select_window(series, window)
        start, stop = window

        if direction == "left":
            if len(w_series):
                return w_series.index.values[-1]
            else:
                start_time = self.getStartTime()
                w_series = sconf.select_window(
                    series,
                    window=(
                        start_time,
                        start))

                if not len(w_series):
                    return None
                else:
                    return w_series.index.values[-1]

        elif direction == "right":
            if len(w_series):
                return w_series.index.values[0]
            else:
                end_time = self.getEndTime()
                w_series = sconf.select_window(series, window=(stop, end_time))

                if not len(w_series):
                    return None
                else:
                    return w_series.index.values[0]
        else:
            raise ValueError("direction should be either left or right")

    def assertSwitch(
            self,
            level,
            from_node,
            to_node,
            window,
            ignore_multiple=True):
        """
        This function asserts that there is context switch from the
           from_node to the to_node:

        Args:
            level (hashable): The level to which the node belongs
            from_node (list): The node from which the task switches out
            to_node (list): The node to which the task switches
            window (tuple): A (start, end) tuple window of time where the
                switch needs to be asserted
            ignore_multiple (bool): If true, the function will ignore multiple
                switches in the window, If false the assert will be true if and
                only if there is a single switch within the specified window

        The function will only return true if and only if there is one
        context switch between the specified nodes
        """

        from_node_index = self._topology.get_index(level, from_node)
        to_node_index = self._topology.get_index(level, to_node)

        agg = self._aggregator(sconf.csum)
        level_result = agg.aggregate(level=level)

        from_node_result = level_result[from_node_index]
        to_node_result = level_result[to_node_index]

        from_time = self._relax_switch_window(from_node_result, "left", window)
        if ignore_multiple:
            to_time = self._relax_switch_window(to_node_result, "left", window)
        else:
            to_time = self._relax_switch_window(
                to_node_result,
                "right", window)

        if from_time and to_time:
            if from_time < to_time:
                return True

        return False

    def getRuntime(self, window=None, percent=False):
        """Returns the Total Runtime of a task

        Args:
            window (tuple): A (start, end) tuple to limit
                the scope of the calculation
            percent (boolean): If True, the result is returned
                as a percentage of the total execution time
                of the run.
        """

        agg = self._aggregator(sconf.residency_sum)
        run_time = agg.aggregate(level="all", window=window)[0]

        if percent:

            if window:
                begin, end = window
                total_time = end - begin
            else:
                total_time = self._run.get_duration()

            run_time = run_time * 100
            run_time = run_time / total_time

        return run_time

    def assertRuntime(
            self,
            expected_value,
            operator,
            window=None,
            percent=False):
        """Assert on the total runtime of the task

         Args:
            expected_value (double): The expected value of the total runtime
            operator (func(a, b)): A binary operator function that
                returns a boolean
            window (tuple): A (start, end) tuple to limit the
                 scope of the calculation
            percent (boolean): If True, the result is returned
                as a percentage of the total execution time of the run.
        """

        run_time = self.getRuntime(window, percent)
        return operator(run_time, expected_value)

    def getPeriod(self, window=None, align="start"):
        """Returns average period of the task in (ms)

         Args:
            window (tuple): A (start, end) tuple to limit the
                 scope of the calculation
            align: "start" aligns period calculation to switch-in events
                "end" aligns the calculation to switch-out events
        """

        agg = self._aggregator(sconf.period)
        period = agg.aggregate(level="all", window=window)[0]
        total, length = map(sum, zip(*period))
        return (total * 1000) / length

    def assertPeriod(
            self,
            expected_value,
            operator,
            window=None,
            align="start"):
        """Assert on the period of the task

         Args:
            expected_value (double): The expected value of the total runtime
            operator (func(a, b)): A binary operator function that
                returns a boolean
            window (tuple): A (start, end) tuple to limit the
                 scope of the calculation
            percent (boolean): If True, the result is returned
                as a percentage of the total execution time of the run.
        """

        period = self.getPeriod(window, align)
        return operator(period, expected_value)

    def getDutyCycle(self, window):
        """Returns the duty cycle of the task
        Args:
             window (tuple): A (start, end) tuple to limit the
                 scope of the calculation

        Duty Cycle:
            The percentage of time the task spends executing
            in the given window
        """

        return self.getRuntime(window, percent=True)

    def assertDutyCycle(self, expected_value, operator, window):
        """
        Args:
            expected_value (double): The expected value of
                the duty cycle
            operator (func(a, b)): A binary operator function that
                returns a boolean
            window (tuple): A (start, end) tuple to limit the
                scope of the calculation

        Duty Cycle:
            The percentage of time the task spends executing
                in the given window
        """
        return self.assertRuntime(
            expected_value,
            operator,
            window,
            percent=True)

    def getFirstCpu(self, window=None):
        """
        Args:
            window (tuple): A (start, end) tuple to limit the
                scope of the calculation
        """

        agg = self._aggregator(sconf.first_cpu)
        result = agg.aggregate(level="cpu", window=window)
        result = list(itertools.chain.from_iterable(result))

        min_time = min(result)
        if math.isinf(min_time):
            return -1
        index = result.index(min_time)
        return self._topology.get_node("cpu", index)[0]

    def assertFirstCpu(self, cpus, window=None):
        """
        Args:
            cpus (int, list): A list of acceptable CPUs
            window (tuple): A (start, end) tuple to limit the scope
                of the calculation
        """
        first_cpu = self.getFirstCpu(window=window)
        cpus = listify(cpus)
        return first_cpu in cpus

    def generate_events(self, level, start_id=0, window=None):
        """Generate events for the trace plot"""

        agg = self._aggregator(sconf.trace_event)
        result = agg.aggregate(level=level, window=window)
        events = []

        for idx, level_events in enumerate(result):
            if not len(level_events):
                continue
            events += np.column_stack((level_events, np.full(len(level_events), idx))).tolist()

        return sorted(events, key = lambda x : x[0])

    def plot(self, level="cpu", window=None, xlim=None):
        """
        Returns:
            trappy.plotter.AbstractDataPlotter
            Call .view() to draw the graph
        """

        if not xlim:
            if not window:
                xlim = [0, self._run.get_duration()]
            else:
                xlim = list(window)

        events = {}
        events[self.name] = self.generate_events(level, window)
        names = [self.name]
        num_lanes = self._topology.level_span(level)
        lane_prefix = level.upper() + ": "
        return trappy.EventPlot(events, names, lane_prefix, num_lanes, xlim)

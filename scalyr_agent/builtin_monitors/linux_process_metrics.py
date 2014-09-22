# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# A ScalyrMonitor that collects metrics on a running Linux process.  The
# collected metrics include CPU and memory usage.
#
# Note, this can be run in standalone mode by:
#     python -m scalyr_agent.run_monitor scalyr_agent.builtin_monitors.linux_process_metrics -c "{ pid:1234}"
#
#   where 1234 is the process id of the target process.
# See documentation for other ways to match processes.
#
# author:  Steven Czerwinski <czerwin@scalyr.com>

__author__ = 'czerwin@scalyr.com'

from scalyr_agent import ScalyrMonitor, BadMonitorConfiguration

from subprocess import Popen, PIPE

import os
import re
import time


class BaseReader:
    """The base class for all readers.  Each derived reader class is responsible for
    collecting a set of statistics from a single per-process file from the /proc file system
    such as /proc/self/stat.  We create an instance for a reader for each application
    that is being monitored.  This instance is created once and then used from
    then on until the monitored process terminates.
    """
    def __init__(self, pid, monitor_id, logger, file_pattern):
        """Initializes the base class.

        @param pid: The id of the process being monitored.
        @param monitor_id: The id of the monitor instance, used to identify all metrics reported through the logger.
        @param logger: The logger instance to use for reporting the metrics.
        @param file_pattern: A pattern that is used to determine the path of the file to be read.  It should
            contain a %d in it which will be replaced by the process id number.  For example, "/proc/%d/stat"

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_logging.AgentLogger
        @type file_pattern: str
        """
        self._pid = pid
        self._id = monitor_id
        self._file_pattern = file_pattern
        # The file object to be read.  We always keep this open and just seek to zero when we need to
        # re-read it.  Some of the /proc files do better with this approach.
        self._file = None
        # The time we last collected the metrics.
        self._timestamp = None
        # True if the reader failed for some unrecoverable error.
        self._failed = False
        self._logger = logger

    def run_single_cycle(self):
        """Runs a single cycle of the sample collection.

        It should read the monitored file and extract all metrics.
        """
        self._timestamp = int(time.time())

        # There are certain error conditions, such as the system not supporting
        # a particular proc file type, that we will never recover from.  So,
        # just always early exit.
        if self._failed:
            return

        filename = self._file_pattern % self._pid

        if self._file is None:
            try:
                self._file = open(filename, "r")
            except IOError, e:
                # We take a simple approach.  If we don't find the file or
                # don't have permissions for it, then just don't collect this
                # stat from now on.  If the user changes the configuration file
                # we will try again to read the file then.
                if e.errno == 13:
                    self._logger.error("The agent does not have permission to read %s.  "
                                       "Maybe you should run it as root.", filename)
                    self._failed = True
                elif e.errno == 2:
                    self._logger.error("The agent cannot read %s.  Your system may not support that proc file type",
                                       filename)
                    self._failed = True
                else:
                    raise e

        if self._file is not None:
            self._file.seek(0)
            self.gather_sample(self._file)

    def gather_sample(self, my_file):
        """Reads the metrics from the file and records them.

        Derived classes must override this method to perform the actual work of
        collecting their specific samples.

        @param my_file: The file to read.
        @type my_file: FileIO
        """
        pass

    def close(self):
        """Closes any files help open by this reader."""
        try:
            self._failed = True
            if self._file is not None:
                self._file.close()
            self._failed = False
        finally:
            self._file = None

    def print_sample(self, metric_name, metric_value, type_value=None):
        """Record the specified metric.

        @param metric_name: The name of the metric.  It should only contain alphanumeric characters,
            periods, underscores.
        @param metric_value: The value of the metric.
        @param type_value: The type of the value.  This is emitted as an extra field for the metric.
            This is often used to label different types of some resource, such as CPU (user CPU vs. system CPU), etc.

        @type metric_name: str
        @type metric_value: float
        @type type_value: str
        """
        # For backward compatibility, we also publish the monitor id as 'app' in all reported stats.  The old
        # Java agent did this and it is important to some dashboards.
        extra = {
            'app': self._id,
        }
        if type_value is not None:
            extra['type'] = type_value

        self._logger.emit_value(metric_name, metric_value, extra)


class StatReader(BaseReader):
    """Reads and records statistics from the /proc/$pid/stat file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.cpu type=user:     number of 1/100ths seconds of user cpu time
      app.cpu type=system:   number of 1/100ths seconds of system cpu time
      app.uptime:            number of milliseconds of uptime
      app.threads:           the number of threads being used by the process
      app.nice:              the nice value for the process
    """
    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/stat")
        # Need the number of jiffies_per_sec for this server to calculate some times.
        self._jiffies_per_sec = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
        # The when this machine was last booted up.  This is required to calculate the process uptime.
        self._boot_time_ms = None

    def __calculate_time_cs(self, jiffies):
        """Returns the number of centiseconds (1/100ths secs) for the given number of jiffies (a weird timing unit
        used the kernel).

        @param jiffies: The number of jiffies.
        @type jiffies: int

        @return: The number of centiseconds for the specified number of jiffies.
        @rtype: int
        """
        return int((jiffies * 100.0) / self._jiffies_per_sec)

    def calculate_time_ms(self, jiffies):
        """Returns the number of milliseconds for the given number of jiffies (a weird timing unit
        used the kernel).

        @param jiffies: The number of jiffies.
        @type jiffies: int

        @return: The number of milliseconds for the specified number of jiffies.
        @rtype: int
        """
        return int((jiffies * 1000.0) / self._jiffies_per_sec)

    def __get_uptime_ms(self):
        """Returns the number of milliseconds the system has been up.

        @return: The number of milliseconds the system has been up.
        @rtype: int
        """
        if self._boot_time_ms is None:
            # We read /proc/uptime once to get the current boot time.
            uptime_file = None
            try:
                uptime_file = open("/proc/uptime", "r")
                # The first number in the file is the number of seconds since
                # boot time.  So, we just use that to calculate the milliseconds
                # past epoch.
                self._boot_time_ms = int(time.time()) * 1000 - int(float(uptime_file.readline().split()[0]) * 1000.0)
            finally:
                if uptime_file is not None:
                    uptime_file.close()

        # Calculate the uptime by just taking current time and subtracting out
        # the boot time.
        return int(time.time()) * 1000 - self._boot_time_ms

    def gather_sample(self, stat_file):
        """Gathers the metrics from the stat file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        """
        # The file format is just a single line of all the fields.
        line = stat_file.readlines()[0]
        # Chop off first part which is the pid and executable file. The
        # executable file is terminated with a paren so just search for that.
        line = line[(line.find(") ")+2):]
        fields = line.split()
        # Then the fields we want are just at fixed field positions in the
        # string.  Just grap them.
        self.print_sample("app.cpu", self.__calculate_time_cs(int(fields[11])), "user")
        self.print_sample("app.cpu",  self.__calculate_time_cs(int(fields[12])), "system")
        # The uptime is calculated by reading the 'start_time from stat which is expressed as the
        # number of jiffies after boot time when this process started.  So, convert away.
        process_uptime = self.__get_uptime_ms() - self.calculate_time_ms(int(fields[19]))
        self.print_sample("app.uptime", process_uptime)
        self.print_sample("app.nice", float(fields[16]))
        self.print_sample("app.threads", int(fields[17]))


class StatusReader(BaseReader):
    """Reads and records statistics from the /proc/$pid/status file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.mem.bytes type=vmsize:        the number of bytes of virtual memory in use
      app.mem.bytes type=resident:      the number of bytes of resident memory in use
      app.mem.bytes type=peak_vmsize:   the maximum number of bytes used for virtual memory for process
      app.mem.bytes type=peak_resident: the maximum number of bytes of resident memory ever used by process
    """
    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/status")

    def gather_sample(self, stat_file):
        """Gathers the metrics from the status file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        """
        for line in stat_file:
            # Each line has a format of:
            # Tag: Value
            #
            # We parse out all lines looking like that and match the stats we care about.
            m = re.search('^(\w+):\s*(\d+)', line)
            if m is None:
                continue

            field_name = m.group(1)
            int_value = int(m.group(2))
            # FDSize is not the same as the number of open file descriptors. Disable
            # for now.
            # if field_name == "FDSize":
            #     self.print_sample("app.fd", int_value)
            if field_name == "VmSize":
                self.print_sample("app.mem.bytes", int_value * 1024,
                                  "vmsize")
            elif field_name == "VmPeak":
                self.print_sample("app.mem.bytes", int_value * 1024,
                                  "peak_vmsize")
            elif field_name == "VmRSS":
                self.print_sample("app.mem.bytes", int_value * 1024,
                                  "resident")
            elif field_name == "VmHWM":
                self.print_sample("app.mem.bytes", int_value * 1024,
                                  "peak_resident")


# Reads stats from /proc/$pid/io.

class IoReader(BaseReader):
    """Reads and records statistics from the /proc/$pid/io file.  Note, this io file is only supported on
    kernels 2.6.20 and beyond, but that kernel has been around since 2007.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.disk.bytes type=read:         the number of bytes read from disk
      app.disk.requests type=read:      the number of disk requests.
      app.disk.bytes type=write:        the number of bytes written to disk
      app.disk.requests type=write:     the number of disk requests.
    """
    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/io")

    def gather_sample(self, stat_file):
        """Gathers the metrics from the io file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        """
        # File format is single value per line with "fieldname:" prefix.
        for x in stat_file:
            fields = x.split()
            if fields[0] == "rchar:":
                self.print_sample("app.disk.bytes", int(fields[1]), "read")
            elif fields[0] == "syscr:":
                self.print_sample("app.disk.requests", int(fields[1]), "read")
            elif fields[0] == "wchar:":
                self.print_sample("app.disk.bytes", int(fields[1]), "write")
            elif fields[0] == "syscw:":
                self.print_sample("app.disk.requests", int(fields[1]), "write")


class NetStatReader(BaseReader):
    """NOTE:  This is not a per-process stat file, so this reader is DISABLED for now.

    Reads and records statistics from the /proc/$pid/net/netstat file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.net.bytes type=in:  The number of bytes read in from the network
      app.net.bytes type=out:  The number of bytes written to the network
      app.net.tcp_retransmits:  The number of retransmits
    """
    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/net/netstat")

    def gather_sample(self, stat_file):
        """Gathers the metrics from the netstate file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        """
        # This file format is weird.  Each set of stats is outputted in two
        # lines.  First, a header line that list the field names.  Then a
        # a value line where each value is specified in the appropriate column.
        # You have to match the column name from the header line to determine
        # what that column's value is.  Also, each pair of lines is prefixed
        # with the same name to make it clear they are tied together.
        all_lines = stat_file.readlines()
        # We will create an array of all of the column names in field_names
        # and all of the corresponding values in field_values.
        field_names = []
        field_values = []

        # To simplify the stats, we add together the two forms of retransmit
        # I could find in the netstats.  Those to fast retransmit Reno and those
        # to selective Ack.
        retransmits = 0
        found_retransmit_metric = False

        # Read over lines, looking at adjacent lines.  If their row names match,
        # then append their column names and values to field_names
        # and field_values.  This will break if the two rows are not adjacent
        # but I do not think that happens in practice.  If it does, we just
        # won't report the stats.
        for i in range(0, len(all_lines) - 1):
            names_split = all_lines[i].split()
            values_split = all_lines[i+1].split()
            # Check the row names are the same.
            if names_split[0] == values_split[0] and len(names_split) == len(values_split):
                field_names.extend(names_split)
                field_values.extend(values_split)

        # Now go back and look for the actual stats we care about.
        for i in range(0, len(field_names)):
            if field_names[i] == "InOctets":
                self.print_sample("app.net.bytes", field_values[i], "in")
            elif field_names[i] == "OutOctets":
                self.print_sample("app.net.bytes", field_values[i], "out")
            elif field_names[i] == "TCPRenoRecovery":
                retransmits += int(field_values[i])
                found_retransmit_metric = True
            elif field_names[i] == "TCPSackRecovery":
                retransmits += int(field_values[i])
                found_retransmit_metric = True

        # If we found both forms of retransmit, add them up.
        if found_retransmit_metric:
            self.print_sample("app.net.tcp_retransmits", retransmits)


class SockStatReader(BaseReader):
    """NOTE:  This is not a per-process stat file, so this reader is DISABLED for now.

    Reads and records statistics from the /proc/$pid/net/sockstat file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.net.sockets_in_use type=*:  The number of sockets in use
    """
    def __init__(self, pid, monitor_id, logger):
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/net/sockstat")

    def gather_sample(self, stat_file):
        """Gathers the metrics from the sockstat file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        """
        for line in stat_file:
            # We just look for the different "inuse" lines and output their
            # socket type along with the count.
            m = re.search('(\w+): inuse (\d+)', line)
            if m is not None:
                self.print_sample("app.net.sockets_in_use", int(m.group(2)),
                                  m.group(1).lower())


class ProcessMonitor(ScalyrMonitor):
    """A Scalyr agent monitor that records metrics about a running process.

    To configure this monitor, you need to provide an id for the instance to identify which process the metrics
    belong to in the logs and a regular expression to match against the list of running processes to determine which
    process should be monitored.

    Example:
      monitors: [{
         module: "builtin_monitors.linux_process_metrics".
         id: "tomcat",
         commandline: "java.*tomcat",
      }]

    Instead of 'commandline', you may also define the 'pid' field which should be set to the id of the process to
    monitor.  However, since ids can change over time, it's better to use the commandline matcher.  The 'pid' field
    is mainly used the linux process monitor run to monitor the agent itself.

    This monitor records the following metrics:
      app.cpu type=user:                the number of 1/100ths seconds of user cpu time
      app.cpu type=system:              the number of 1/100ths seconds of system cpu time
      app.uptime:                       the number of milliseconds of uptime
      app.threads:                      the number of threads being used by the process
      app.nice:                         the nice value for the process
      app.mem.bytes type=vmsize:        the number of bytes of virtual memory in use
      app.mem.bytes type=resident:      the number of bytes of resident memory in use
      app.mem.bytes type=peak_vmsize:   the maximum number of bytes used for virtual memory for process
      app.mem.bytes type=peak_resident: the maximum number of bytes of resident memory ever used by process
      app.disk.bytes type=read:         the number of bytes read from disk
      app.disk.requests type=read:      the number of disk requests.
      app.disk.bytes type=write:        the number of bytes written to disk
      app.disk.requests type=write:     the number of disk requests.

    In additional to the fields listed above, each metric will also have a field 'app' set to the monitor id to specify
    which process the metric belongs to.

    You can run multiple instances of this monitor per agent to monitor different processes.
    """
    def _initialize(self):
        """Performs monitor-specific initialization."""
        # The id of the process being monitored, if one has been matched.
        self.__pid = None
        # The list of BaseReaders instantiated to gather metrics for the process.
        self.__gathers = []

        self.__id = self._config.get('id', required_field=True, convert_to=str)
        self.__commandline_matcher = self._config.get('commandline', default=None, convert_to=str)
        self.__target_pid = self._config.get('pid', default=None, convert_to=str)

        if self.__commandline_matcher is None and self.__target_pid is None:
            raise BadMonitorConfiguration('At least one of the following fields must be provide: commandline or pid',
                                          'commandline')

        # Make sure to set our configuration so that the proper parser is used.
        self.log_config = {
            'parser': 'agent-metrics',
            'path': 'linux_process_metrics.log',
        }

    def __set_process(self, pid):
        """Sets the id of the process for which this monitor instance should record metrics.

        @param pid: The process id or None if there is no process to monitor.
        @type pid: int or None
        """
        if self.__pid is not None:
            for gather in self.__gathers:
                gather.close()

        self.__pid = pid
        self.__gathers = []

        if pid is not None:
            self.__gathers.append(StatReader(self.__pid, self.__id, self._logger))
            self.__gathers.append(StatusReader(self.__pid, self.__id, self._logger))
            self.__gathers.append(IoReader(self.__pid, self.__id, self._logger))
        # TODO: Re-enable these if we can find a way to get them to truly report
        # per-app statistics.
        #        self.gathers.append(NetStatReader(self.pid, self.id, self._logger))
        #        self.gathers.append(SockStatReader(self.pid, self.id, self._logger))

    def gather_sample(self):
        """Collect the per-process metrics for the monitored process.

        If the process is no longer running, then attempts to match a new one.
        """
        if self.__pid is not None and not self.__is_running():
            self.__set_process(None)

        if self.__pid is None:
            self.__set_process(self.__select_process())

        for gather in self.__gathers:
            gather.run_single_cycle()

    def __select_process(self):
        """Returns the proces id of a running process that fulfills the match criteria.

        This will either use the commandline matcher or the target pid to find the process.
        If no process is matched, None is returned.

        @return: The process id of the matching process, or None
        @rtype: int or None
        """
        sub_proc = None
        if self.__commandline_matcher is not None:
            try:
                # Spawn a process to run ps and match on the command line.  We only output two
                # fields from ps.. the pid and command.
                sub_proc = Popen(['ps', 'ax', '-o', 'pid,command'],
                                 shell=False, stdout=PIPE)

                sub_proc.stdout.readline()
                for line in sub_proc.stdout:
                    line = line.strip()
                    if line.find(' ') > 0:
                        pid = int(line[:line.find(' ')])
                        line = line[(line.find(' ') + 1):]
                        if re.search(self.__commandline_matcher, line) is not None:
                            return pid
                return None
            finally:
                # Be sure to wait on the spawn process.
                if sub_proc is not None:
                    sub_proc.wait()
        else:
            # See if the specified target pid is running.  If so, then return it.
            try:
                # Special case '$$' to mean this process.
                if self.__target_pid == '$$':
                    pid = os.getpid()
                else:
                    pid = int(self.__target_pid)
                os.kill(pid, 0)
                return pid
            except OSError:
                # If we get this, it means we tried to signal a process we do not have permission to signal.
                # If this is the case, we won't have permission to read its stats files either, so we ignore it.
                return None

    def __is_running(self):
        """Returns true if the current process is still running.

        @return:  True if the monitored process is still running.
        @rtype: bool
        """
        try:
            os.kill(self.__pid, 0)
            return True
        except OSError, e:
            # Errno #3 corresponds to the process not running.  We could get
            # other errors like this process does not have permission to send
            # a signal to self.pid.  But, if that error is returned to us, we
            # know the process is running at least, so we ignore the error.
            return e.errno != 3


__all__ = [ProcessMonitor]
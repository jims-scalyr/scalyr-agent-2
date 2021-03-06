# Copyright 2014, Scalyr, Inc.
#
# Note, this can be run in standalone mode by:
# python -m scalyr_agent.run_monitor
# scalyr_agent.builtin_monitors.apache_monitor
import httplib
import urllib2
import socket
import urlparse

from scalyr_agent import ScalyrMonitor, define_config_option, define_log_field, define_metric

httpSourceAddress = "127.0.0.1"

__monitor__ = __name__

define_config_option(__monitor__, 'module',
                     'Always ``scalyr_agent.builtin_monitors.apache_monitor``',
                     convert_to=str, required_option=True)
define_config_option(__monitor__, 'status_url',
                     'Optional (defaults to \'http://localhost/server-status/?auto\').  The URL the monitor will fetch'
                     'to retrieve the Apache status information.', default='http://localhost/server-status/?auto')
define_config_option(__monitor__, 'source_address',
                     'Optional (defaults to \'%s\'). The IP address to be used as the source address when fetching '
                     'the status URL.  Many servers require this to be 127.0.0.1 because they only server the status '
                     'page to requests from localhost.' % httpSourceAddress, default=httpSourceAddress)
define_config_option(__monitor__, 'id',
                     'Optional (defaults to empty string).  Included in each log message generated by this monitor, '
                     'as a field named ``instance``. Allows you to distinguish between different Apache instances '
                     'running on the same server.', convert_to=str)

define_log_field(__monitor__, 'monitor', 'Always ``apache_monitor``.')
define_log_field(__monitor__, 'metric', 'The metric name.  See the metric tables for more information.')
define_log_field(__monitor__, 'value', 'The value of the metric.')
define_log_field(__monitor__, 'instance', 'The ``id`` value from the monitor configuration.')

define_metric(__monitor__, 'apache.connections.active', 'The number of connections currently opened to the server.')
define_metric(__monitor__, 'apache.connections.writing', 'The number of connections currently writing to the clients.')
define_metric(__monitor__, 'apache.connections.idle', 'The number of connections currently idle/sending keep alives.')
define_metric(__monitor__, 'apache.connections.closing', 'The number of connections currently closing.')
define_metric(__monitor__, 'apache.workers.active', 'How many workers are currently active.')
define_metric(__monitor__, 'apache.workers.idle', 'How many of the workers are currently idle.')


# Taken from:
#   http://stackoverflow.com/questions/1150332/source-interface-with-python-and-urllib2
#
# For connecting to local machine, specifying the source IP may be required.  So, using
# this mechanism should allow that.  Since getting status requires "opening up" a
# non-standard/user-facing web page, it is best to be cautious.
#
# Note - the use of a global is ugly, but this form is more compatible than with another
# method mentioned which would not require the global.  (The cleaner version was added
# in Python 2.7.)
class BindableHTTPConnection(httplib.HTTPConnection):

    def connect(self):
        """Connect to the host and port specified in __init__."""
        self.sock = socket.socket()
        self.sock.bind((self.source_ip, 0))
        if isinstance(self.timeout, float):
            self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))


def BindableHTTPConnectionFactory(source_ip):
    def _get(host, port=None, strict=None, timeout=0):
        bhc = BindableHTTPConnection(
            host,
            port=port,
            strict=strict,
            timeout=timeout)
        bhc.source_ip = source_ip
        return bhc
    return _get


class BindableHTTPHandler(urllib2.HTTPHandler):

    def http_open(self, req):
        return self.do_open(
            BindableHTTPConnectionFactory(httpSourceAddress), req)


class ApacheMonitor(ScalyrMonitor):
    """
# Apache Monitor

The Apache monitor allows you to collect data about the usage and performance of your Apache server.

Each monitor can be configured to monitor a specific Apache instance, thus allowing you to configure alerts and the
dashboard entries independently (if desired) for each instance.

## Configuring Apache

In order to enable the status module, you must update the ``VirtualHost`` configuration section of your Apache server.
The file that contains your ``VirtualHost`` configuration is dependent on your version of Apache as well as your
site's individual set up.  Please see the
[reference documentation for the status module](http://httpd.apache.org/docs/2.2/mod/mod_status.html)
for general instructions.  For example, for Linux systems, the ``/etc/apache2/sites-available`` directory typically
contains the file with the ``VirtualHost`` configuration.

To enable the status module, add the following to the ``VirtualHost`` configuration section (between ``<VirtualHost>``
 and ``</VirtualHost>``):

    <Location /server-status>
       SetHandler server-status
       Order deny,allow
       Deny from all
       Allow from 127.0.0.1
    </Location>

This block does a couple of very important things.  First, it specifies that the status page will be exposed on the
local server at ``http://<address>/server-status``.  The next three statements work together and are probably the most
important for security purposes.  ``Order allow,deny`` tells Apache how to check things to grant access.
``Deny from all`` is a blanket statement to disallow access by everyone.  ``Allow from 127.0.0.1`` tells Apache to
allow access to the page from localhost.

Another important step is to make sure that the status module is enabled.  This will vary based on the operating system
the server is running.  Here is the documentation for a few popular sytems:
[CentOS 5](https://www.centos.org/docs/5/html/5.1/Deployment_Guide/s1-apache-addmods.html)
[Ubuntu 14.04](https://help.ubuntu.com/14.04/serverguide/httpd.html)

For example, on Ubuntu (and other Debian-based variants),  you can check to see if the module is enabled by invoking
the command:

    ls /etc/apache2/mods-enabled

If you see ``status.conf`` and ``status.load`` present, the module is enabled.  If you need to enable it, issuing the
following command should take care of that (for apache2):

    sudo /usr/sbin/a2enmod status

The process for enabling the module and specific locations for the configuration changes will vary from platform to
platform.  Additionally, if Apache was compiled manually, the module may or may not be available.  Consult the
particulars of your platform.

Once you make the configuration change, you will need to restart Apache.

## Configuring the Scalyr Apache Monitor

The Apache monitor is included with the Scalyr agent.  In order to configure it, you will need to add its monitor
configuration to the Scalyr agent config file.

A basic Apache monitor configuration entry might resemble:

    monitors: [
      {
          module: "scalyr_agent.builtin_monitors.apache_monitor",
      }
    ]

If you were running an instances of Apache on a non-standard port (say 8080), your config might resemble:

    monitors: [
      {
          module: "scalyr_agent.builtin_monitors.apache_monitor",
          status_url: "http://localhost:8080/server-status"
          id: "customers"
      }
    ]

Note the "id" field in the configurations.  This is an optional field that allows you to specify an identifier specific
to a particular instance of Apache and will make it easier to filter on metrics specific to that instance.
    """
    def _initialize(self):
        global httpSourceAddress
        self.__url = self._config.get('status_url',
                                      default='http://localhost/server-status/?auto')
        self.__sourceaddress = self._config.get('source_addresss',
                                                default=httpSourceAddress)
        httpSourceAddress = self.__sourceaddress

    def _parse_data(self, data):
        fields = {
            "Total Accesses:": "total_accesses",
            "Total kBytes:": "total_kbytes_sent",
            "Uptime:": "uptime",
            "ReqPerSec:": "request_per_sec",
            "BytesPerSec:": "bytes_per_sec",
            "BytesPerReq:": "bytes_per_req",
            "BusyWorkers:": "busy_workers",
            "IdleWorkers:": "idle_workers",
            "ConnsTotal:": "connections_total",
            "ConnsAsyncWriting:": "async_connections_writing",
            "ConnsAsyncKeepAlive:": "async_connections_keep_alive",
            "ConnsAsyncClosing:": "async_connections_closing",
        }
        result = {}
        lines = data.splitlines()
        i = 0
        # skip any blank lines
        while len(lines[i]) == 0:
            i = i + 1
        while i < len(lines):
            for key in fields:
                if lines[i].startswith(key):
                    values = lines[i].split()
                    result[fields[key]] = values[1]
            i = i + 1
        return result

    def _get_status(self):
        data = None
        # verify that the URL is valid
        try:
            url = urlparse.urlparse(self.__url)
        except Exception as e:
            self._logger.error(
                "The URL configured for requesting the status page appears to be invalid.  Please verify that the URL is correct in your monitor configuration.  The specified url: %s" %
                self.__url)
            return data
        # attempt to request server status
        try:
            opener = urllib2.build_opener(BindableHTTPHandler)
            handle = opener.open(self.__url)
            data = handle.read()
            if data is not None:
                data = self._parse_data(data)
        except urllib2.HTTPError as err:
            message = "An HTTP error occurred attempting to retrieve the status.  Please consult your server logs to determine the cause.  HTTP error code: ", err.code
            if err.code == 404:
                message = "The URL used to request the status page appears to be incorrect.  Please verify the correct URL and update your apache_monitor configuration."
            elif err.code == 403:
                message = "The server is denying access to the URL specified for requesting the status page.  Please verify that permissions to access the status page are correctly configured in your server configuration and that your apache_monitor configuration reflects the same configuration requirements."
            elif err.code >= 500 or err.code < 600:
                message = "The server failed to fulfill the request to get the status page.  Please consult your server logs to determine the cause.  HTTP error code: ", err.code
            self._logger.error(message)
            data = None
        except urllib2.URLError as err:
            message = "The was an error attempting to reach the server.  Make sure the server is running and properly configured.  The error reported is: ", err
            if err.reason.errno == 111:
                message = "The HTTP server does not appear to running or cannot be reached.  Please check that it is running and is reachable at the address: %s" % url.netloc
            self._logger.error(message)
            data = None
        except Exception as e:
            self._logger.error(
                "An error occurred attempting to request the server status: %s" %
                e)
            data = None
        return data

    """
    # Currently disabled as it requires platform specific functionality.  This will need
    # be reactivated once a cross platform solution is implemented.
    def _get_procinfo(self):
        try:
            data = subprocess.Popen("ps aux | grep apache | grep -v grep | grep -v scalyr | awk '{print $2, $3, $4}'", shell=True, stdout=subprocess.PIPE).stdout.read()
            result = {}
            lines = data.splitlines()
            i = 0
            while i < len(lines):
                if len(lines[i]) != 0:
                    values = lines[i].split()
                    if len(values) == 3:
                        result[values[0]] = {
                            "cpu": values[1],
                            "mem": values[2]
                        }
                i = i + 1
        except Exception, e:
            self._logger.error("Unable to check process status: %s" % e)
            result = None
        return result
    """

    def gather_sample(self):
        data = self._get_status()
        if data is None:
            self._logger.error("No data returned.")
        else:
            samplesToEmit = {
                "busy_workers": 'apache.workers.active',
                "idle_workers": 'apache.workers.idle',
                "connections_total": 'apache.connections.active',
                "async_connections_writing": 'apache.connections.writing',
                "async_connections_keep_alive": 'apache.connections.idle',
                "async_connections_closing": 'apache.connections.closing'
            }

            for key in samplesToEmit:
                if key in data:
                    self._logger.emit_value(samplesToEmit[key], int(data[key]))

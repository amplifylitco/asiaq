"""
Top level disco_aws_automation module.  Orchestrates deployment to AWS.
"""
from ConfigParser import NoOptionError
from collections import defaultdict
import getpass
import logging
import time
from datetime import datetime
import dateutil.parser

import boto
import boto.ec2
import boto.ec2.autoscale
from boto.exception import EC2ResponseError

from .disco_log_metrics import DiscoLogMetrics
from .disco_elb import DiscoELB, DiscoELBPortConfig
from .disco_alarm import DiscoAlarm
from .disco_group import DiscoGroup
from .disco_aws_util import (
    is_truthy,
    get_instance_launch_time,
    size_as_recurrence_map,
    size_as_minimum_int_or_none,
    size_as_maximum_int_or_none
)
from .disco_bake import DiscoBake
from .disco_constants import (
    DEFAULT_CONFIG_SECTION,
    DEFAULT_INSTANCE_TYPE,
    SMOKETEST_POLL_INTERVAL,
    SMOKETEST_TIMEOUT,
    AUTOSCALE_POLL_INTERVAL,
    AUTOSCALE_TIMEOUT,
)
from .disco_remote_exec import DiscoRemoteExec
from .disco_storage import DiscoStorage
from .disco_vpc import DiscoVPC
from .resource_helper import (
    keep_trying,
    wait_for_state,
    throttled_call
)
from .exceptions import (
    AMIError,
    VPCEnvironmentError,
    SmokeTestError,
    CommandError,
    TimeoutError,
    ProgrammerError,
)

logger = logging.getLogger(__name__)


class DiscoAWS(object):
    '''Class orchestrating deployment on AWS'''

    # Too many arguments, but we want to mock a lot of things out, so...
    # pylint: disable=too-many-arguments
    def __init__(self, config, environment_name=None, boto2_conn=None, vpc=None, remote_exec=None,
                 storage=None, discogroup=None, elb=None,
                 log_metrics=None, alarms=None):

        if not environment_name and not vpc:
            raise ProgrammerError("Either 'vpc' or 'environment_name' must always be specified.")
        elif environment_name:
            self.environment_name = environment_name
        else:
            self.environment_name = vpc.environment_name
        self._config = config
        self._project_name = self._config.get("disco_aws", "project_name")
        self._connection = boto2_conn or None  # lazily initialized
        self._vpc = vpc or None  # lazily initialized
        self._disco_remote_exec = remote_exec or None  # lazily initialized
        self._disco_storage = storage or None  # lazily initialized
        self._discogroup = discogroup or None  # lazily initialized
        self._elb = elb or None  # lazily initialized
        self._log_metrics = log_metrics or None  # lazily initialized
        self._alarms = alarms or None  # lazily initialized

    @property
    def connection(self):
        """Lazily creates boto2 ec2 connection"""
        if not self._connection:
            self._connection = boto.connect_ec2()
        return self._connection

    @property
    def disco_storage(self):
        """Lazily creates disco storage object"""
        if not self._disco_storage:
            self._disco_storage = DiscoStorage(self.environment_name, self.connection)
        return self._disco_storage

    @property
    def discogroup(self):
        """Lazily creates disco group object"""
        if not self._discogroup:
            self._discogroup = DiscoGroup(environment_name=self.environment_name)
        return self._discogroup

    @property
    def log_metrics(self):
        """Lazily creates disco log metrics object"""
        if not self._log_metrics:
            self._log_metrics = DiscoLogMetrics(environment=self.environment_name)
        return self._log_metrics

    @property
    def elb(self):
        """Lazily creates ELB connection for our current VPC"""
        if not self._elb:
            self._elb = DiscoELB(self.vpc)
        return self._elb

    @property
    def alarms(self):
        """Lazily creates alarms object for our current VPC"""
        if not self._alarms:
            self._alarms = DiscoAlarm(self.environment_name)
        return self._alarms

    @property
    def vpc(self):
        """VPC we are currently operating in"""
        if not self._vpc and self.environment_name not in ("none", "None", "-", ""):
            self._vpc = DiscoVPC.fetch_environment(environment_name=self.environment_name)
        if not self._vpc:
            raise VPCEnvironmentError("Failed to select environment: {0}.".format(self.environment_name))
        return self._vpc

    @property
    def disco_remote_exec(self):
        '''Lazily creates a remote execution class'''
        if not self._disco_remote_exec and self.vpc:
            buckets = self.vpc.get_credential_buckets(self._project_name)
            self._disco_remote_exec = DiscoRemoteExec(buckets)
        return self._disco_remote_exec

    def config(self, option, section=DEFAULT_CONFIG_SECTION, default=None):
        """Get a value from the config"""
        env_option = "{0}@{1}".format(option, self.environment_name)
        if self._config.has_option(section, env_option):
            return self._config.get(section, env_option)
        if self._config.has_option(section, option):
            return self._config.get(section, option)

        return default

    def config_no_default(self, option, section=DEFAULT_CONFIG_SECTION):
        """
        Similar to config function above except that it doesnt return a default when an option is not found.
        Raises a NoOptionError error when option not found.
        """
        env_option = "{0}@{1}".format(option, self.environment_name)
        return self._config.get(section, env_option) \
            if self._config.has_option(section, env_option) else self._config.get(section, option)

    def hostclass_option(self, hostclass, option):
        # TODO swap hostclass (default to DEFAULT_CONFIG_SECTION) and option
        """Fetch a hostclass configuration option, if it does not exist get the default"""
        env_option = "{0}@{1}".format(option, self.environment_name)
        if self._config.has_option(hostclass, env_option):
            return self._config.get(hostclass, env_option)
        elif self._config.has_option(hostclass, option):
            return self._config.get(hostclass, option)

        return self.config_no_default(section=DEFAULT_CONFIG_SECTION, option="default_{0}".format(option))

    def hostclass_option_default(self, hostclass, option, default=None):
        """Fetch a hostclass configuration option if it exists, otherwise return value passed in as default"""
        try:
            return self.hostclass_option(hostclass, option)
        except NoOptionError:
            return default

    def create_userdata(self, hostclass, owner, is_spotinst=False):
        """This is autoscaling group specific user data"""
        fixed_ip_hostclass = {}
        data = {}

        fixed_ip_hostclass['http_proxy'] = self.config("http_proxy_hostclass")
        fixed_ip_hostclass['zookeeper'] = self.config("zookeeper_hostclass")
        fixed_ip_hostclass['logger'] = self.config("logger_hostclass")
        fixed_ip_hostclass['logforwarder'] = self.config("logforwarder_hostclass")

        data["hostclass"] = hostclass
        if is_truthy(self.hostclass_option_default(hostclass, "enable_proxy")):
            data["http_proxy_ip"] = self._get_hostclass_ip_address(
                fixed_ip_hostclass['http_proxy'], ""
            )
        data["logger_ip"] = self._get_hostclass_ip_address(
            fixed_ip_hostclass['logger'], ""
        )
        data["logforwarder_ip"] = self._get_hostclass_ip_address(
            fixed_ip_hostclass['logforwarder'], ""
        )
        data["environment_name"] = self.vpc.environment_name
        data["domain_name"] = self.hostclass_option_default(hostclass, "domain_name")
        data["owner"] = owner or getpass.getuser()
        data["credential_buckets"] = " ".join(self.vpc.get_credential_buckets(self._project_name))
        data["zookeepers"] = "[\\\"mhczookeeper-{0}.{1}:2181\\\"]".format(
            data["environment_name"], data["domain_name"])
        data["eip"] = self.hostclass_option_default(hostclass, "eip")
        data["floating_ips"] = self._get_hostclass_ip_address(hostclass, "")
        data["floating_ip"] = data["floating_ips"].split()[0] if data["floating_ips"] else ""
        smoketest_termination = is_truthy(self.hostclass_option(hostclass, "smoketest_termination"))
        data["smoketest_termination"] = "1" if smoketest_termination else "0"
        data["project_name"] = self.config("project_name")
        data["is_spotinst"] = "1" if is_spotinst else "0"
        logger.debug("userdata: %s", data)
        return data

    @staticmethod
    def _nonify(value):
        return None if (isinstance(value, basestring) and value.lower() == "none") or not value else value

    def get_instance_type(self, hostclass):
        """Pull in instance_type existing launch configuration"""
        old_config = self.discogroup.get_launch_config(hostclass=hostclass)
        return old_config['instance_type'] if old_config else DEFAULT_INSTANCE_TYPE

    def get_meta_network_by_name(self, meta_network_name):
        """Return meta network instance by meta network name"""
        if meta_network_name not in self.vpc.networks:
            raise VPCEnvironmentError(
                "Could not locate required subnets {0} in vpc. Check config."
                .format(meta_network_name)
            )
        return self.vpc.networks[meta_network_name]

    def get_meta_network(self, hostclass):
        """Return meta network instance by hostclass"""
        return self.get_meta_network_by_name(self.hostclass_option(hostclass, "meta_network"))

    def get_subnets(self, meta_network, hostclass):
        """
        Restrict Autoscaling subnet if we need specific private IPs,
        otherwise use all subnets in meta network.
        """
        subnet_ips = self._get_hostclass_ip_address(hostclass)

        if not subnet_ips:
            return [disco_subnet.subnet_dict for disco_subnet in meta_network.disco_subnets.values()]

        return [meta_network.subnet_by_ip(subnet_ip) for subnet_ip in subnet_ips.split(' ')]

    def get_block_device_mappings(self, hostclass, ami=None,
                                  extra_space=None, extra_disk=None, iops=None,
                                  instance_type=None):
        """
        Create new BDM if parameters are specified, else create a device
        mapping from existing configuration.
        """
        return [self.disco_storage.configure_storage(
            hostclass=hostclass, ami_id=ami.id,
            extra_space=extra_space, extra_disk=extra_disk, iops=iops,
            ephemeral_disk_count=min([self.disco_storage.get_ephemeral_disk_count(i_type)
                                      for i_type in instance_type.split(':')]))]

    def create_floating_interfaces(self, meta_network, hostclass):
        """Creates any floating interfaces as needed"""
        # create floating interfaces if needed.
        subnet_ips = self._get_hostclass_ip_address(hostclass)
        if subnet_ips:
            for subnet_ip in subnet_ips.split(' '):
                meta_network.get_interface(subnet_ip)

    def create_scaling_schedule(self, min_size, desired_size, max_size, hostclass=None, group_name=None):
        """Create autoscaling schedule"""
        self.discogroup.delete_all_recurring_group_actions(hostclass=hostclass, group_name=group_name)
        maps = [
            size_as_recurrence_map(min_size, sentinel=None),
            size_as_recurrence_map(desired_size, sentinel=None),
            size_as_recurrence_map(max_size, sentinel=None),
        ]
        times = set([item for sublist in [a_map.keys() for a_map in maps] for item in sublist])
        combined_map = {time: [maps[0].get(time), maps[1].get(time), maps[2].get(time)]
                        for time in times if time is not None}
        for recurrence, sizes in combined_map.items():
            self.discogroup.create_recurring_group_action(
                str(recurrence), hostclass=hostclass, group_name=group_name,
                min_size=sizes[0], desired_capacity=sizes[1], max_size=sizes[2])

    # Pylint thinks that we have too many local variables, but we needs them.
    # pylint:  disable=too-many-locals
    def update_elb(self, hostclass, update_autoscaling=True, testing=False):
        '''Creates, Updates and Delete an ELB for a hostclass depending on current configuration'''
        if not is_truthy(self.hostclass_option_default(hostclass, "elb", "False")):
            if self.elb.get_elb(hostclass):
                self.elb.delete_elb(hostclass)
            elb = None
        else:
            elb_meta_network_name = self.hostclass_option_default(hostclass, "elb_meta_network", None)
            if elb_meta_network_name:
                elb_meta_network = self.get_meta_network_by_name(elb_meta_network_name)
                elb_subnets = elb_meta_network.disco_subnets.values()
                subnet_ids = [disco_subnet.subnet_dict['SubnetId'] for disco_subnet in elb_subnets]
            else:
                elb_meta_network = self.get_meta_network(hostclass)
                elb_subnets = self.get_subnets(elb_meta_network, hostclass)
                subnet_ids = [subnet['SubnetId'] for subnet in elb_subnets]

            elb_dns_alias = self.hostclass_option_default(hostclass, "elb_dns_alias") if not testing else None
            elb = self.elb.get_or_create_elb(
                hostclass,
                security_groups=[elb_meta_network.security_group.id],
                subnets=subnet_ids,
                hosted_zone_name=self.hostclass_option_default(hostclass, "domain_name"),
                health_check_url=self.hostclass_option_default(hostclass, "elb_health_check_url"),
                port_config=DiscoELBPortConfig.from_config(self, hostclass),
                elb_public=is_truthy(self.hostclass_option_default(hostclass, "elb_public", "no")),
                elb_dns_alias=elb_dns_alias,
                sticky_app_cookie=self.hostclass_option_default(hostclass, "elb_sticky_app_cookie", None),
                idle_timeout=int(self.hostclass_option_default(hostclass, "elb_idle_timeout", 300)),
                connection_draining_timeout=int(self.hostclass_option_default(hostclass,
                                                                              "elb_connection_draining",
                                                                              300)),
                cert_name=self.hostclass_option_default(hostclass, "elb_cert_name"),
                testing=testing,
                tags={
                    "hostclass": hostclass,
                    "is_testing": "1" if testing else "0",
                    "environment": self.environment_name,
                    "productline": self.hostclass_option_default(hostclass, 'product_line', 'unknown')
                },
                cross_zone_load_balancing=is_truthy(
                    self.hostclass_option_default(
                        hostclass,
                        "elb_cross_zone_load_balancing",
                        "true"
                    )
                )
            )

        if update_autoscaling:
            self.discogroup.update_elb([elb['LoadBalancerName']] if elb else [], hostclass=hostclass)

        return elb

    def provision(self, ami, hostclass=None, owner=None, instance_type=None, monitoring_enabled=False,
                  extra_space=None, extra_disk=None, iops=None, no_destroy=False, min_size=None,
                  desired_size=None, max_size=None, testing=False, termination_policies=None, chaos=None,
                  create_if_exists=False, group_name=None, spotinst=False, spotinst_reserve=None):
        # TODO move key, instance_type, monitoring enabled, extra_space, extra_disk into config file.
        # Pylint thinks this function has too many arguments and too many local variables
        # pylint: disable=R0913, R0914
        """
        Instantiate AMIs

        If one of min_size, max_size or desired_size is specified the host is created with
        an autoscaling group.

        Keyword arguments:
        ami -- the image to start instances of
        owner -- used to tag the instance so we know who started an instance
        instance_type -- the Amazon instance type, m3.large, t2.small, etc.
        monitoring_enabled -- tells Amazon that we want detailed cloudwatch monitoring
        extra_space -- the number of extra GB to allocate on the root disk
        extra_disk -- the number of GB to allocate in an additional disk
        iops is -- the number of provision IOPS to request for the additional disk
        no_destroy -- when set this command will not destroy a host that fails to boot
        min_size -- the minimum size of the autoscaling group
        max_size -- the maximum size of the autoscaling group
        desired_size -- the currently desired size of for the autoscaling group
        testing -- bring up host in testing mode (for CI)
        chaos -- when true we want these instances to be terminatable by the chaos process
        create_if_exists -- create a new autoscaling group even if one already exists
        group_name -- force reuse of an existing autoscaling group
        spotinst -- use AWS autoscaling group or Spotinst elastigroup
        """
        # It's possible that the ami isn't available yet, so wait here
        wait_for_state(ami, u'available', 600)
        # TODO is it necessary to wait here???

        is_spotinst = is_truthy(str(spotinst)) or is_truthy(self.config('spotinst', hostclass))

        meta_network = self.get_meta_network(hostclass)
        instance_type = instance_type if instance_type else self.get_instance_type(hostclass)

        user_data = self.create_userdata(hostclass, owner, is_spotinst=is_spotinst)

        block_device_mappings = self.get_block_device_mappings(
            hostclass, ami, extra_space, extra_disk, iops, instance_type
        )

        self.log_metrics.update(hostclass)

        self.create_floating_interfaces(meta_network, hostclass)

        chaos = is_truthy(chaos or self.hostclass_option_default(hostclass, "chaos", "True"))

        elb = self.update_elb(hostclass, update_autoscaling=False, testing=testing)

        tags = {
            "hostclass": hostclass,
            "application": hostclass,
            "owner": user_data["owner"],
            "environment": self.environment_name,
            "environment_class": self.vpc.environment_class,
            "chaos": chaos,
            "is_testing": "1" if testing else "0"
        }

        hostclass_option_tags = self.hostclass_option_default(hostclass, "tags")
        if hostclass_option_tags:
            for tag in hostclass_option_tags.split(','):
                for key, value in [tag.split(':')]:
                    tags[key.strip()] = value.strip()

        target_groups = []
        if is_truthy(self.hostclass_option_default(hostclass, "target_group", "False")):
            target_groups = self.elb.get_or_create_target_group(
                environment=self.environment_name,
                hostclass=hostclass,
                port_config=DiscoELBPortConfig.from_config(self, hostclass),
                vpc_id=self.vpc.get_vpc_id(),
                health_check_path=self.hostclass_option_default(hostclass, "elb_health_check_url"),
                tags=tags
            )

        group = self.discogroup.create_or_update_group(
            hostclass=hostclass,
            image_id=ami.id,
            subnets=self.get_subnets(meta_network, hostclass),
            key_name=DiscoAWS._nonify(self.hostclass_option(hostclass, "ssh_key_name")),
            min_size=size_as_minimum_int_or_none(min_size),
            max_size=size_as_maximum_int_or_none(max_size),
            desired_size=size_as_maximum_int_or_none(desired_size),
            instance_profile_name=self.hostclass_option_default(hostclass, "instance_profile_name"),
            ebs_optimized=self.disco_storage.is_ebs_optimized(instance_type),
            security_groups=[meta_network.security_group.id],
            tags=tags,
            user_data="\n".join(['{0}="{1}"'.format(key, value) for key, value in user_data.iteritems()]),
            associate_public_ip_address=is_truthy(self.hostclass_option(hostclass, "public_ip")),
            instance_monitoring=monitoring_enabled,
            instance_type=instance_type,
            load_balancers=[elb['LoadBalancerName']] if elb else [],
            target_groups=target_groups,
            block_device_mappings=block_device_mappings,
            create_if_exists=create_if_exists,
            termination_policies=termination_policies,
            group_name=group_name,
            spotinst=is_spotinst,
            spotinst_reserve=spotinst_reserve
        )

        self.create_scaling_schedule(min_size, desired_size, max_size, group_name=group['name'])

        # Create alarms and custom metrics for the hostclass, if is not being used for testing
        if not testing:
            self.alarms.create_alarms(hostclass, group['name'])

        logger.info("Spun up %s instances of %s from %s into group %s",
                    size_as_maximum_int_or_none(desired_size), hostclass, ami.id, group['name'])

        return {
            "hostclass": hostclass,
            "no_destroy": no_destroy,
            "group_name": group['name'],
            "chaos": chaos,
            "spotinst": is_truthy(str(spotinst) or self.config('spotinst', hostclass))
        }

    def stop(self, instances):
        """ Stop (aka shutdown) instances """
        return self._stop(instances)

    def terminate(self, instances, use_autoscaling=False):
        """ Stop instance and destroy EBS volume """
        return self._stop(instances, terminate=True, use_autoscaling=use_autoscaling)

    def _stop(self, instances, terminate=False, use_autoscaling=False):
        """
        Stop / terminate instances depending on value of terminate parameter.
        """
        logger.debug("_stop: %s terminate=%s use_autoscaling=%s", instances, terminate, use_autoscaling)

        instances = [i for i in instances if i.state != u'terminated']
        instance_ids = [i.id for i in instances]
        if instance_ids:
            if terminate:
                for instance in instances:
                    self.vpc.delete_instance_routes(instance)
                    if use_autoscaling:
                        self.discogroup.terminate(instance.id)
                if not use_autoscaling:
                    throttled_call(self.connection.terminate_instances, instance_ids)
                logger.info("terminated: %s", instances)
            else:
                throttled_call(self.connection.stop_instances, instance_ids)
                logger.info("stopped: %s", instances)
        else:
            logger.info("No unterminated instances")

        return instances

    def find_jump_host(self):
        """
        Returns the best available ssh jump host.

        This returns the first instance matching jump_box_hostclasses. If there are multiple
        running and smoketested instances in the first matching hostclass, then one with a
        public IP is preferred over one without one.
        """
        jump_hostclasses = self.config("jump_box_hostclasses").split(' ')
        for hostclass in jump_hostclasses:
            running_hosts = [host for host in self.instances_from_hostclass(hostclass)
                             if DiscoAWS.is_running(host) and host.tags.get("smoketest")]
            public_hosts = [host for host in running_hosts if host.ip_address]
            best_host = public_hosts[0] if public_hosts else running_hosts[0] if running_hosts else None
            if best_host:
                return best_host
        return None

    def find_jump_address(self):
        """Returns IPv4 address of ssh jump host"""
        host = self.find_jump_host()
        return None if not host else host.ip_address if host.ip_address else host.private_ip_address

    def remotecmd(self, instance, *args, **kwargs):
        """
        remotecmd accepts a boto instance followed by a list containing a string
        of all arguments, starting with the program to run.

        remotecmd optionally accepts four additional named arguments:

        stdin -- the bytes to send into program input
        nothrow -- when True the method will not throw if the program returns a non-zero result.

        In addition to these explicit arguments, this method will redirect the
        subprocesses's stderr to stdout, and capture stdout.  If the logging level
        is set to debug, it will log the captured output.

        Returns a tuple of (return_code, captured_output).

        examples:

        self.remotecmd(inst, ['cat - > /tmp/myfile'], stdin='my content')

        ret, out = self.remotecmd(inst, ['ls -l /etc'], nothrow=True)

        """
        jump_address = self.find_jump_address()
        address = instance.private_ip_address
        if not address:
            raise CommandError("No private IP address available to ssh to")
        return self.disco_remote_exec.remotecmd(address, jump_address=jump_address, *args, **kwargs)

    def instances(self, filters=None, instance_ids=None):
        """
        Return all instances or subset as specified by filter.

        Filter documentation:
        http://docs.aws.amazon.com/AWSEC2/latest/APIReference/ApiReference-query-DescribeInstances.html
        """
        combined_filters = {}
        if filters:
            combined_filters.update(filters)
        if self.vpc:
            vpc_filter = {tag.get('Name'): tag.get('Values')[0] for tag in self.vpc.vpc_filters()}
            combined_filters.update(vpc_filter)
        reservations = keep_trying(
            60, self.connection.get_all_instances,
            filters=combined_filters, instance_ids=instance_ids
        )
        return [instance
                for reservation in reservations
                for instance in reservation.instances
                if self.vpc or not instance.vpc_id]

    def instance_from_hostname(self, hostname):
        """Returns first instance with particular hostname"""
        instances = self.instances(filters={"tag:hostname": hostname})
        if instances:
            return instances[0]
        else:
            raise ValueError("Could not find an instance with hostname %s" % hostname)

    def instances_from_hostclass(self, hostclass):
        """Returns a flat list of all instances of a particular hostclass"""
        return self.instances_from_hostclasses([hostclass])

    def instances_from_hostclasses(self, hostclasses):
        """Returns a flat list of all instances for a list of hostclasses"""
        return [
            instance
            for instance in self.instances()
            if instance.tags.get("hostclass", "-") in hostclasses
        ]

    def instances_from_amis(self, ami_ids, group_name=None, launch_time=None):
        """
        Returns instances matching any of a list of AMI ids and filtered by ASG name and Launch Time
        :param ami_ids: List of AMI IDs used to select the list of returned instances
        :param group_name: The ASG name, If the group name is specified, only instances belonging to the
        group will be returned.
        :param launch_time: If launch time is specified only instances launched on or after the specified
        launch time will be returned.
        :return: List of instances.
        """
        instance_ids_in_group = None
        if group_name:
            instance_ids_in_group = [inst.id for inst in self.instances_from_asgs([group_name])]

        instances = self.instances(filters={"image_id": ami_ids}, instance_ids=instance_ids_in_group)
        if launch_time is None:
            return instances

        # Filter the instance by launch_time
        return [
            instance
            for instance in instances
            if get_instance_launch_time(instance) >= launch_time
        ]

    def instances_from_asgs(self, asgs):
        """Returns instances matching any of a list of autoscaling group names"""
        return [
            instance
            for instance in self.instances()
            if instance.tags.get("aws:autoscaling:groupName", "-") in asgs
        ]

    def spindown(self, hostclasses):
        """
        Shuts down hosts in all hostclasses in list.

        .. warning:: This currently does a dirty shutdown, no attempt is made to preserve logs.
        """
        for hostclass in hostclasses:
            self.discogroup.delete_groups(hostclass=hostclass, force=True)

            self.elb.delete_elb(hostclass)

            self.alarms.delete_hostclass_environment_alarms(
                self.environment_name, hostclass
            )

            self.log_metrics.delete_metrics(hostclass)
            self.log_metrics.delete_log_groups(hostclass)

    def spinup(self, hostclass_dicts, stage=None, no_smoke=False, testing=False, create_if_exists=False,
               group_name=None):
        # Pylint thinks this function has too many local variables
        # pylint: disable=R0914,R0912
        """
        Provisions a complete pipeline.
        Hosts are spun up in sequential groups, where each group spins up in parallel.

        The pipeline should be defined in this format:
        hostclass_dicts = [
            { "sequence": 1,
              "hostclass": "mhcdiscosomething",
              "desired_size": 1,
              "instance_type": "m1.large",
              "extra_space": None,
              "extra_disk": None,
              "iops": None,
              "smoke_test": "true",
              "ami": None,
              "min_size": None,
              "max_size": None,
              "termination_policies": None,
              "chaos": "yes",
              "spotinst": False,
              "spotinst_reserve": "15%"
              },
            ...]
        """
        # If AMI specified lookup hostclass from AMI else lookup AMI from hostclass
        stage = stage if stage else self.vpc.ami_stage()
        bake = DiscoBake(self._config, self.connection)
        for entry in hostclass_dicts:
            entry["ami_obj"] = bake.find_ami(stage, entry.get("hostclass"), entry.get("ami"),
                                             include_private=False)
            if not entry["ami_obj"]:
                raise AMIError(
                    "Couldn't find AMI {0} for hostclass {1}, aborting spinup.".format(
                        entry.get("ami"), entry.get("hostclass")))
            entry["hostclass"] = DiscoBake.ami_hostclass(entry["ami_obj"])

        # determine which subset of hostclasses will need smoke testing
        flammable = set([]) if no_smoke else set(
            [
                hdict["hostclass"]
                for hdict in hostclass_dicts
                if is_truthy(hdict.get("smoke_test", ""))
            ]
        )

        # group by sequence number and run groups sequentially
        groups = set([int(hdict["sequence"]) for hdict in hostclass_dicts])
        for group in sorted(list(groups)):
            hostclass_iter = (
                (hdict["hostclass"], hdict.get("termination_policies"), hdict)
                for hdict in hostclass_dicts
                if int(hdict["sequence"]) == group
            )

            # spinup all hostclasses within the same group in parallel
            metadata = [
                self.provision(
                    ami=hdict["ami_obj"],
                    hostclass=hostclass,
                    instance_type=hdict.get("instance_type") or self.get_instance_type(hdict["hostclass"]),
                    extra_space=int(hdict["extra_space"]) if hdict.get("extra_space") else None,
                    extra_disk=int(hdict["extra_disk"]) if hdict.get("extra_disk") else None,
                    iops=int(hdict["iops"]) if hdict.get("iops") else None,
                    min_size=hdict.get("min_size"), max_size=hdict.get("max_size"),
                    desired_size=hdict.get("desired_size"), testing=testing,
                    termination_policies=termination_policies.split() if termination_policies else None,
                    chaos=hdict.get("chaos"),
                    create_if_exists=create_if_exists,
                    group_name=group_name,
                    spotinst=hdict.get("spotinst"),
                    spotinst_reserve=hdict.get('spotinst_reserve')
                )
                for (hostclass, termination_policies, hdict) in hostclass_iter]

            if metadata[0]:
                self.smoketest(self.wait_for_autoscaling_instances(
                    [_hc for _hc in metadata if _hc["hostclass"] in flammable]))

    @staticmethod
    def _instance_count_lt_min_size(group, all_instances):
        group_instances = [_i for _i in all_instances if _i['group_name'] == group['name']]
        return len(group_instances) < group['min_size']

    def wait_for_autoscaling_instances(self, metadata_list, timeout=AUTOSCALE_TIMEOUT):
        """
        Wait for autoscaling groups to spinup, returns instance id's of spun up machines
        """
        start_time = time.time()
        max_time = start_time + timeout

        name_to_group = {group['name']: group for group in self.discogroup.get_existing_groups()}
        yet_to_scale = group_names = set([meta["group_name"] for meta in metadata_list])

        while True:
            auto_instances = self.discogroup.get_instances()
            logger.debug("yet_to_scale: %s", yet_to_scale)
            yet_to_scale = [gname for gname in yet_to_scale
                            if DiscoAWS._instance_count_lt_min_size(name_to_group[gname], auto_instances)]
            if not yet_to_scale or (time.time() >= max_time):
                break
            logger.info("Waiting for %s autoscaling groups to reach min_size", len(yet_to_scale))
            time.sleep(AUTOSCALE_POLL_INTERVAL)

        if yet_to_scale:
            raise TimeoutError(
                "Timed out waiting for {0} to reach autoscale min_size after {1}s."
                .format(" ".join([gname for gname in yet_to_scale]), timeout))

        if metadata_list:
            logger.info("Waited for %s autoscaling groups to reach min_size in %s seconds",
                        len(metadata_list), int(0.5 + time.time() - start_time))

        instance_ids = [instance['instance_id'] for instance in auto_instances
                        if instance['group_name'] in group_names]

        return self.instances(instance_ids=instance_ids) if instance_ids else []

    def wait_for_autoscaling(self, ami_id, min_count, timeout=AUTOSCALE_TIMEOUT, group_name=None,
                             launch_time=None):
        """
        Wait for at least min_count instances of a particular AMI to spin up.
        raises TimeoutError if min_count hosts do not exist by timeout seconds
        """
        start_time = time.time()
        max_time = start_time + timeout

        while time.time() < max_time:
            instances = self.instances_from_amis([ami_id], launch_time=launch_time, group_name=group_name)
            if len(instances) >= min_count:
                return
            time.sleep(AUTOSCALE_POLL_INTERVAL)

        raise TimeoutError(
            "Timed out waiting for {} {} to hosts to spin up after {}s."
            .format(min_count, ami_id, timeout))

    def smoketest(self, instance_list, timeout=SMOKETEST_TIMEOUT):
        """
        Repeatedly smoketests instances in list until they all pass.
        raises TimeoutError if all hosts do not pass by timeout seconds
        """
        yet_to_pass = instance_list
        start_time = time.time()
        max_time = start_time + timeout

        while True:
            smokey = []
            logger.debug("yet_to_pass: %s", yet_to_pass)
            for instance in yet_to_pass:
                try:
                    self.smoketest_once(instance)
                except TimeoutError:
                    smokey.append(instance)
            yet_to_pass = smokey
            if not yet_to_pass or (time.time() >= max_time):
                break
            logger.info("Waiting for %s host[s] to pass smoke test", len(yet_to_pass))
            time.sleep(SMOKETEST_POLL_INTERVAL)

        if yet_to_pass:
            raise TimeoutError(
                "Timed out waiting for {0} to pass smoketest after {1}s."
                .format(" ".join([inst.id for inst in yet_to_pass]), timeout))

        if instance_list:
            logger.info("Smoke tested %s host[s] in %s seconds",
                        len(instance_list), int(0.5 + time.time() - start_time))

    def smoketest_once(self, instance):
        """
        Runs smoke test of one host once
        """
        try:
            if DiscoAWS.is_terminal_state(instance):
                raise SmokeTestError(
                    "Terminal smoketest error, {0} is in terminal state {1}."
                    .format(instance, instance.state))
            if not instance.tags.get("smoketest"):
                raise TimeoutError("Instance %s hasn't passed smoketests yet." % instance.id)
        except EC2ResponseError as err:
            if err.code == "InvalidInstanceID.NotFound":
                raise TimeoutError("AWS doesn't think %s exists yet" % instance.id)
            else:
                raise
        return True

    @staticmethod
    def is_running(instance):
        """Returns true if an instance is in the running state"""
        instance.update()
        return instance.state == u'running'

    @staticmethod
    def is_terminal_state(instance):
        """Returns true if an instance is in the terminated state"""
        instance.update()
        return instance.state in (u'failed', u'terminated')

    @staticmethod
    def instance_age(instance):
        """
        Number of seconds since instance was launched
        """
        now = datetime.utcnow()
        launch_time = dateutil.parser.parse(instance.launch_time).replace(tzinfo=None)
        return (now - launch_time).total_seconds()

    def promote_running_instances_to_prod(self, seconds):
        """
        Promote, to prod, AMIs of instances that have been running for at least
        this many seconds.
        """
        running_instances = self.instances(filters={"instance-state-name": "running"})
        running_amis = defaultdict(list)
        for instance in running_instances:
            running_amis[instance.image_id].append(instance)

        long_running_ami_ids = [
            ami
            for ami, instances in running_amis.iteritems()
            if min([
                DiscoAWS.instance_age(instance)
                for instance in instances
            ]) > seconds
        ]

        if not long_running_ami_ids:
            logger.warning("No running instances with sufficient uptime, to promote AMIs.")
            return

        disco_bake = DiscoBake()
        amis = disco_bake.get_amis(long_running_ami_ids)
        for ami in amis:
            logger.debug("ami: %s", ami.id)
            if ami.tags['stage'] == disco_bake.final_stage:
                disco_bake.promote_ami_to_production(ami)

    def _get_hostclass_ip_address(self, hostclass, default=None):
        """
        Backwards compatible way to get the ip addresses for a hostclass.

        lookup either "ip_addresses" or "ip_address" in the config
        """
        ip_address = self.hostclass_option_default(hostclass, "ip_address", default)

        if not ip_address:
            return ip_address
        elif ip_address.startswith("-") or ip_address.startswith("+"):
            meta_network = self.get_meta_network(hostclass)
            return str(meta_network.ip_by_offset(ip_address))

        return ip_address

    def get_default_meta_network(self, default=None):
        """Get the default meta network from config or None if not in config"""
        return self.config('default_meta_network', default=default)

    def get_default_domain_name(self, default=None):
        """Get the default domain name from config or None if not in config"""
        return self.config('default_domain_name', default=default)

    def get_default_product_line(self, default=None):
        """Get the product line from config or None if not in config"""
        return self.config('default_product_line', default=default)

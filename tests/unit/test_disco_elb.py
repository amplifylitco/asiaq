"""Tests of disco_elb"""
from unittest import TestCase
from mock import MagicMock, ANY
from moto import mock_elb
from boto.exception import EC2ResponseError
from disco_aws_automation import DiscoELB
from disco_aws_automation.disco_elb import DiscoELBPortConfig, DiscoELBPortMapping

TEST_ENV_NAME = 'unittestenv'
TEST_HOSTCLASS = 'mhcunit'
TEST_VPC_ID = 'vpc-56e10e3d'  # the hard coded VPC Id that moto will always return
TEST_DOMAIN_NAME = 'test.example.com'
TEST_CERTIFICATE_ARN_ACM = "arn:aws:acm::123:blah"
TEST_CERTIFICATE_ARN_IAM = "arn:aws:acm::123:blah"
# With these constants, you could do some significant testing of setting and clearing stickiness policies,
# were it not for the fact that moto's ELB support is insufficient for the task.
MOCK_POLICY_NAME = "mock-sticky-policy"
MOCK_APP_STICKY_POLICY = {
    u'PolicyAttributeDescriptions': [{u'AttributeName': 'CookieName', u'AttributeValue': 'JSESSIONID'}],
    u'PolicyName': MOCK_POLICY_NAME,
    u'PolicyTypeName': 'AppCookieStickinessPolicyType'
}

MOCK_ELB_STICKY_POLICY = {
    u'PolicyAttributeDescriptions': [{u'AttributeName': 'CookieExpirationPeriod', u'AttributeValue': '0'}],
    u'PolicyName': MOCK_POLICY_NAME,
    u'PolicyTypeName': 'LBCookieStickinessPolicyType'
}

# This is not a constant I am 100% comfortable with, but it appears to be reproducible so far
MOCK_ELB_ADDRESS = 'd0aff1d22a42200a1c35825e61137bb4.us-east-1.elb.amazonaws.com'


def _get_vpc_mock():
    vpc_mock = MagicMock()
    vpc_mock.environment_name = TEST_ENV_NAME
    vpc_mock.get_vpc_id.return_value = TEST_VPC_ID
    return vpc_mock


class DiscoELBTests(TestCase):
    """Test DiscoELB"""

    def setUp(self):
        self.route53 = MagicMock()
        self.acm = MagicMock()
        self.iam = MagicMock()
        self.elb2 = MagicMock()
        self.disco_elb = DiscoELB(
            _get_vpc_mock(), route53=self.route53, acm=self.acm, iam=self.iam, elb2=self.elb2
        )
        self.acm.get_certificate_arn.return_value = TEST_CERTIFICATE_ARN_ACM
        self.iam.get_certificate_arn.return_value = TEST_CERTIFICATE_ARN_IAM

    # pylint: disable=too-many-arguments, R0914
    def _create_elb(
            self,
            hostclass=TEST_HOSTCLASS,
            public=False,
            tls=False,
            instance_protocols=('HTTP',),
            instance_ports=(80,),
            elb_protocols=('HTTP',),
            elb_ports=(80,),
            idle_timeout=None,
            connection_draining_timeout=None,
            sticky_app_cookie=None,
            elb_dns_alias=None,
            existing_cookie_policy=None,
            testing=False,
            cross_zone_load_balancing=True,
            cert_name=None,
            health_check_url='/'
    ):
        sticky_policies = [existing_cookie_policy] if existing_cookie_policy else []
        mock_describe = MagicMock(return_value={'PolicyDescriptions': sticky_policies})
        self.disco_elb.elb_client.describe_load_balancer_policies = mock_describe

        elb_protocols = ['HTTPS'] if tls else elb_protocols
        elb_ports = [443] if tls else elb_ports

        return self.disco_elb.get_or_create_elb(
            hostclass=hostclass or TEST_HOSTCLASS,
            security_groups=['sec-1'],
            subnets=[],
            hosted_zone_name=TEST_DOMAIN_NAME,
            health_check_url=health_check_url,
            port_config=DiscoELBPortConfig(
                [
                    DiscoELBPortMapping(internal_port, internal_protocol, external_port, external_protocol)
                    for (internal_port, internal_protocol), (external_port, external_protocol) in zip(
                        zip(instance_ports, instance_protocols),
                        zip(elb_ports, elb_protocols)
                    )
                ]
            ),
            elb_public=public,
            sticky_app_cookie=sticky_app_cookie,
            elb_dns_alias=elb_dns_alias,
            idle_timeout=idle_timeout,
            connection_draining_timeout=connection_draining_timeout,
            cert_name=cert_name,
            tags={
                'environment': TEST_ENV_NAME,
                'hostclass': hostclass,
                'is_testing': '1' if testing else '0'
            },
            cross_zone_load_balancing=cross_zone_load_balancing
        )

    @mock_elb
    def test_get_certificate_arn_prefers_acm(self):
        '''get_certificate_arn() prefers an ACM provided certificate'''
        self.assertEqual(self.disco_elb.get_certificate_arn("dummy"), TEST_CERTIFICATE_ARN_ACM)

    @mock_elb
    def test_get_certificate_arn_fallback_to_iam(self):
        '''get_certificate_arn() uses an IAM certificate if no ACM cert available'''
        self.acm.get_certificate_arn.return_value = None
        self.assertEqual(self.disco_elb.get_certificate_arn("dummy"), TEST_CERTIFICATE_ARN_IAM)

    @mock_elb
    def test_get_cname(self):
        '''Make sure get_cname returns what we expect'''
        self.assertEqual(self.disco_elb.get_cname(TEST_HOSTCLASS, TEST_DOMAIN_NAME),
                         "mhcunit-unittestenv.test.example.com")

    @mock_elb
    def test_get_elb_with_create(self):
        """Test creating a ELB"""
        self._create_elb()
        self.assertEqual(
            len(self.disco_elb.elb_client.describe_load_balancers()['LoadBalancerDescriptions']), 1)

    @mock_elb
    def test_get_elb_with_update(self):
        """Updating an ELB doesn't add create a new ELB"""
        self._create_elb()
        self._create_elb()
        self.assertEqual(
            len(self.disco_elb.elb_client.describe_load_balancers()['LoadBalancerDescriptions']), 1)

    @mock_elb
    def test_get_elb_internal(self):
        """Test creation an internal private ELB"""
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)
        self._create_elb()
        self.disco_elb.elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'HTTP',
                'LoadBalancerPort': 80,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80
            }],
            Subnets=[],
            SecurityGroups=['sec-1'],
            Scheme='internal')

    @mock_elb
    def test_get_elb_internal_no_tls(self):
        """Test creation an internal private ELB"""
        self.acm.get_certificate_arn.return_value = None
        self.iam.get_certificate_arn.return_value = None
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)
        self._create_elb()
        elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'HTTP',
                'LoadBalancerPort': 80,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80
            }],
            Subnets=[],
            SecurityGroups=['sec-1'],
            Scheme='internal')

    @mock_elb
    def test_get_elb_external(self):
        """Test creation a publically accessible ELB"""
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)
        self._create_elb(public=True)
        elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'HTTP',
                'LoadBalancerPort': 80,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80
            }],
            Subnets=[],
            SecurityGroups=['sec-1'])

    @mock_elb
    def test_get_elb_with_tls(self):
        """Test creation an ELB with TLS"""
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)
        self._create_elb(tls=True)
        elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'HTTPS',
                'LoadBalancerPort': 443,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80,
                'SSLCertificateId': TEST_CERTIFICATE_ARN_ACM
            }],
            Subnets=[],
            SecurityGroups=['sec-1'],
            Scheme='internal')

    @mock_elb
    def test_get_elb_with_tls_and_cert_name(self):
        """Test creation an ELB with TLS and a specific cert name"""
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)

        def _get_certificate_arn(name):
            return 'arn:aws:acm::foo:com' if name == 'foo.com' else TEST_CERTIFICATE_ARN_ACM

        self.acm.get_certificate_arn.side_effect = _get_certificate_arn

        self._create_elb(tls=True, cert_name='foo.com')
        elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'HTTPS',
                'LoadBalancerPort': 443,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80,
                'SSLCertificateId': 'arn:aws:acm::foo:com'
            }],
            Subnets=[],
            SecurityGroups=['sec-1'],
            Scheme='internal'
        )

    @mock_elb
    def test_get_elb_cert_name_not_found(self):
        """Test creation an ELB with TLS and a specific cert name that doesn't exist"""
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)

        def _get_certificate_arn(name):
            return None if name == 'foo.com' else TEST_CERTIFICATE_ARN_ACM

        self.acm.get_certificate_arn.side_effect = _get_certificate_arn
        self.iam.get_certificate_arn.return_value = None

        self._create_elb(tls=True, cert_name='foo.com')
        elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'HTTPS',
                'LoadBalancerPort': 443,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80,
                'SSLCertificateId': ''
            }],
            Subnets=[],
            SecurityGroups=['sec-1'],
            Scheme='internal'
        )

    @mock_elb
    def test_get_elb_with_tcp(self):
        """Test creation an ELB with TCP"""
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)
        self._create_elb(instance_protocols=['TCP'], instance_ports=[25],
                         elb_protocols=['TCP'], elb_ports=[25])
        elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'TCP',
                'LoadBalancerPort': 25,
                'InstanceProtocol': 'TCP',
                'InstancePort': 25
            }],
            Subnets=[],
            SecurityGroups=['sec-1'],
            Scheme='internal')

    @mock_elb
    def test_get_elb_with_multiple_ports(self):
        """Test creating an ELB that listens on multiple ports"""
        elb_client = self.disco_elb.elb_client
        elb_client.create_load_balancer = MagicMock(wraps=elb_client.create_load_balancer)
        self._create_elb(instance_protocols=['HTTP', 'HTTP'], instance_ports=[80, 80],
                         elb_protocols=['HTTP', 'HTTPS'], elb_ports=[80, 443])
        elb_client.create_load_balancer.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            Listeners=[{
                'Protocol': 'HTTP',
                'LoadBalancerPort': 80,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80
            }, {
                'Protocol': 'HTTPS',
                'LoadBalancerPort': 443,
                'InstanceProtocol': 'HTTP',
                'InstancePort': 80,
                'SSLCertificateId': TEST_CERTIFICATE_ARN_ACM
            }],
            Subnets=[],
            SecurityGroups=['sec-1'],
            Scheme='internal')

    @mock_elb
    def test_get_elb_http_health_check(self):
        """
        Creating an ELB creates a health check for the first HTTP or HTTPS instance listener
        """
        # HTTP first
        elb_client = self.disco_elb.elb_client
        elb_client.configure_health_check = MagicMock(wraps=elb_client.configure_health_check)
        self._create_elb(
            instance_protocols=['HTTP', 'HTTP'],
            instance_ports=[80, 80],
            elb_protocols=['HTTP', 'HTTPS'],
            elb_ports=[80, 443],
            health_check_url='/health/check/endpoint/'
        )

        elb_client.configure_health_check.assert_called_once_with(
            LoadBalancerName=ANY,
            HealthCheck={
                'Target': 'HTTP:80/health/check/endpoint/',
                'Interval': 5,
                'Timeout': 4,
                'UnhealthyThreshold': 2,
                'HealthyThreshold': 2
            }
        )

        # HTTPS first
        elb_client = self.disco_elb.elb_client
        elb_client.configure_health_check = MagicMock(wraps=elb_client.configure_health_check)
        self._create_elb(
            instance_protocols=['HTTPS', 'HTTP'],
            instance_ports=[443, 80],
            elb_protocols=['HTTP', 'HTTPS'],
            elb_ports=[80, 443],
            health_check_url='/health/check/endpoint/'
        )

        elb_client.configure_health_check.assert_called_once_with(
            LoadBalancerName=ANY,
            HealthCheck={
                'Target': 'HTTPS:443/health/check/endpoint/',
                'Interval': 5,
                'Timeout': 4,
                'UnhealthyThreshold': 2,
                'HealthyThreshold': 2
            }
        )

        # HTTP after TCP
        elb_client.configure_health_check = MagicMock(wraps=elb_client.configure_health_check)
        self._create_elb(
            instance_protocols=['TCP', 'HTTP'],
            instance_ports=[9001, 4271],
            elb_protocols=['TCP', 'HTTPS'],
            elb_ports=[9001, 443],
            health_check_url='/health/check/endpoint/'
        )

        elb_client.configure_health_check.assert_called_once_with(
            LoadBalancerName=ANY,
            HealthCheck={
                'Target': 'HTTP:4271/health/check/endpoint/',
                'Interval': 5,
                'Timeout': 4,
                'UnhealthyThreshold': 2,
                'HealthyThreshold': 2
            }
        )

        # HTTPS after TCP
        elb_client.configure_health_check = MagicMock(wraps=elb_client.configure_health_check)
        self._create_elb(
            instance_protocols=['TCP', 'HTTPS'],
            instance_ports=[9001, 4271],
            elb_protocols=['TCP', 'HTTPS'],
            elb_ports=[9001, 443],
            health_check_url='/health/check/endpoint/'
        )

        elb_client.configure_health_check.assert_called_once_with(
            LoadBalancerName=ANY,
            HealthCheck={
                'Target': 'HTTPS:4271/health/check/endpoint/',
                'Interval': 5,
                'Timeout': 4,
                'UnhealthyThreshold': 2,
                'HealthyThreshold': 2
            }
        )

    @mock_elb
    def test_get_elb_tcp_health_check(self):
        """
        If there are no HTTP(S) instance listeners, create_elb creates a health check for the first listener
        """
        # HTTP first
        elb_client = self.disco_elb.elb_client
        elb_client.configure_health_check = MagicMock(wraps=elb_client.configure_health_check)
        self._create_elb(
            instance_protocols=['TCP', 'TCP'],
            instance_ports=[9001, 9002],
            elb_protocols=['TCP', 'TCP'],
            elb_ports=[9001, 9002],
            health_check_url='/health/check/endpoint/'
        )

        elb_client.configure_health_check.assert_called_once_with(
            LoadBalancerName=ANY,
            HealthCheck={
                'Target': 'TCP:9001',
                'Interval': 5,
                'Timeout': 4,
                'UnhealthyThreshold': 2,
                'HealthyThreshold': 2
            }
        )

    @mock_elb
    def test_get_elb_with_idle_timeout(self):
        """Test creating an ELB with an idle timeout"""
        client = self.disco_elb.elb_client
        client.modify_load_balancer_attributes = MagicMock(wraps=client.modify_load_balancer_attributes)

        self._create_elb(idle_timeout=100)

        client.modify_load_balancer_attributes.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            LoadBalancerAttributes={'ConnectionDraining': {'Enabled': False, 'Timeout': 0},
                                    'ConnectionSettings': {'IdleTimeout': 100},
                                    'CrossZoneLoadBalancing': {'Enabled': True}}
        )

    @mock_elb
    def test_get_elb_with_connection_draining(self):
        """Test creating ELB with connection draining"""
        client = self.disco_elb.elb_client
        client.modify_load_balancer_attributes = MagicMock(wraps=client.modify_load_balancer_attributes)

        self._create_elb(connection_draining_timeout=100)

        client.modify_load_balancer_attributes.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            LoadBalancerAttributes={
                'ConnectionDraining': {'Enabled': True, 'Timeout': 100},
                'CrossZoneLoadBalancing': {'Enabled': True}
            }
        )

    @mock_elb
    def test_get_elb_no_cross_zone_lb(self):
        """Test creating ELB without cross zone load balancing"""
        client = self.disco_elb.elb_client
        client.modify_load_balancer_attributes = MagicMock(wraps=client.modify_load_balancer_attributes)

        self._create_elb(cross_zone_load_balancing=False)

        client.modify_load_balancer_attributes.assert_called_once_with(
            LoadBalancerName=DiscoELB.get_elb_id('unittestenv', 'mhcunit'),
            LoadBalancerAttributes={
                'ConnectionDraining': {'Enabled': False, 'Timeout': 0},
                'CrossZoneLoadBalancing': {'Enabled': False}
            }
        )

    @mock_elb
    def test_delete_elb(self):
        """Test deleting an ELB"""
        self._create_elb()
        self.disco_elb.delete_elb(TEST_HOSTCLASS)
        load_balancers = self.disco_elb.elb_client.describe_load_balancers()['LoadBalancerDescriptions']
        self.assertEqual(len(load_balancers), 0)

    @mock_elb
    def test_get_existing_elb(self):
        """Test get_elb for a hostclass"""
        self._create_elb()
        self.assertIsNotNone(self.disco_elb.get_elb(TEST_HOSTCLASS))

    @mock_elb
    def test_list(self):
        """Test getting the list of ELBs"""
        self._create_elb(hostclass='mhcbar')
        self._create_elb(hostclass='mhcfoo')
        self.assertEqual(len(self.disco_elb.list()), 2)

    @mock_elb
    def test_elb_delete(self):
        """Test deletion of ELBs"""
        self._create_elb(hostclass='mhcbar')
        self.disco_elb.delete_elb(hostclass='mhcbar')
        self.assertEqual(len(self.disco_elb.list()), 0)

    @mock_elb
    def test_destroy_all_elbs(self):
        """Test deletion of all ELBs"""
        self._create_elb(hostclass='mhcbar')
        self._create_elb(hostclass='mhcfoo')
        self.disco_elb.destroy_all_elbs()
        self.assertEqual(len(self.disco_elb.list()), 0)

    @mock_elb
    def test_wait_for_instance_health(self):
        """Test that we can wait for instances attached to an ELB to enter a specific state"""
        self._create_elb(hostclass='mhcbar')
        elb_id = self.disco_elb.get_elb_id(TEST_ENV_NAME, 'mhcbar')
        instances = [{"InstanceId": "i-123123aa"}]
        self.disco_elb.elb_client.register_instances_with_load_balancer(LoadBalancerName=elb_id,
                                                                        Instances=instances)
        self.disco_elb.wait_for_instance_health_state(hostclass='mhcbar')

    @mock_elb
    def test_tagging_elb(self):
        """Test tagging an ELB"""
        client = self.disco_elb.elb_client
        client.add_tags = MagicMock(wraps=client.add_tags)

        self._create_elb()

        client.add_tags.assert_called_once_with(
            LoadBalancerNames=[DiscoELB.get_elb_id('unittestenv', 'mhcunit')],
            Tags=[
                {'Key': 'environment', 'Value': TEST_ENV_NAME},
                {'Key': 'is_testing', 'Value': '0'},
                {'Key': 'hostclass', 'Value': TEST_HOSTCLASS},
            ]
        )

    @mock_elb
    def test_display_listing(self):
        """ Test that the tags for an ELB are correctly read for display """
        self._create_elb(hostclass='mhcbar')
        self._create_elb(hostclass='mhcfoo', testing=True)

        listings = self.disco_elb.list_for_display()

        elb_names = [listing['elb_name'] for listing in listings]

        self.assertEqual(set(['unittestenv-mhcbar', 'unittestenv-mhcfoo-test']), set(elb_names))

    @mock_elb
    def test_default_dns_alias(self):
        """Test that the default DNS alias is set up"""
        self._create_elb(hostclass='mhcfunky')
        self.route53.create_record.assert_called_once_with(
            TEST_DOMAIN_NAME,
            'mhcfunky-' + TEST_ENV_NAME + '.' + TEST_DOMAIN_NAME, 'CNAME', MOCK_ELB_ADDRESS)

    @mock_elb
    def test_custom_dns_alias(self):
        """Test that the custom DNS alias is set up"""
        self._create_elb(hostclass='mhcfunky', elb_dns_alias='thefunk')
        self.route53.create_record.assert_any_call(
            TEST_DOMAIN_NAME,
            'thefunk' + '.' + TEST_DOMAIN_NAME, 'CNAME', MOCK_ELB_ADDRESS)
        self.route53.create_record.assert_any_call(
            TEST_DOMAIN_NAME,
            'mhcfunky-' + TEST_ENV_NAME + '.' + TEST_DOMAIN_NAME, 'CNAME', MOCK_ELB_ADDRESS)

    def test_get_target_group(self):
        """Test getting a target group"""
        describe_call = {
            "TargetGroups": [
                {"TargetGroupArn": "mock_target_group"},
            ]
        }
        self.elb2.describe_target_groups.return_value = describe_call
        group = self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
        )
        self.assertEqual(group, ["mock_target_group"])

    def test_create_tg_without_port_or_health(self):
        """Test creating a group without port config or health check"""
        self.elb2.describe_target_groups.side_effect = EC2ResponseError(
            status="mockstatus",
            reason="mockreason"
        )
        self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
        )
        self.elb2.create_target_group.assert_called_with(
            Name="unittestenv-mhcunit",
            Protocol='HTTP',
            Port=80,
            VpcId=TEST_VPC_ID,
            HealthCheckProtocol="HTTP",
            HealthCheckPort="80",
            HealthCheckPath="/"
        )

    def test_create_tg_with_health_check(self):
        """Test creating a group with health check"""
        self.elb2.describe_target_groups.side_effect = EC2ResponseError(
            status="mockstatus",
            reason="mockreason"
        )
        self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
            health_check_path="/mockpath"
        )
        self.elb2.create_target_group.assert_called_with(
            Name="unittestenv-mhcunit",
            Protocol='HTTP',
            Port=80,
            VpcId=TEST_VPC_ID,
            HealthCheckProtocol="HTTP",
            HealthCheckPort="80",
            HealthCheckPath="/mockpath"
        )

    def test_create_target_group_with_port_config(self):
        """Test creating a group using port config"""
        self.elb2.describe_target_groups.side_effect = EC2ResponseError(
            status="mockstatus",
            reason="mockreason"
        )

        instance_protocols = ('HTTP',)
        instance_ports = (80,)
        elb_protocols = ('HTTP',)
        elb_ports = (80,)

        self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
            port_config=DiscoELBPortConfig(
                [
                    DiscoELBPortMapping(internal_port, internal_protocol, external_port, external_protocol)
                    for (internal_port, internal_protocol), (external_port, external_protocol) in zip(
                        zip(instance_ports, instance_protocols), zip(elb_ports, elb_protocols))
                ]
            )
        )
        self.elb2.create_target_group.assert_called_with(
            Name="unittestenv-mhcunit",
            Protocol='HTTP',
            Port=80,
            VpcId=TEST_VPC_ID,
            HealthCheckProtocol="HTTP",
            HealthCheckPort="80",
            HealthCheckPath="/"
        )

    def test_create_target_group_tcp(self):
        """Test creating a group non http/https"""
        self.elb2.describe_target_groups.side_effect = EC2ResponseError(
            status="mockstatus",
            reason="mockreason"
        )

        instance_protocols = ('TCP',)
        instance_ports = (80,)
        elb_protocols = ('TCP',)
        elb_ports = (80,)

        self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
            port_config=DiscoELBPortConfig(
                [
                    DiscoELBPortMapping(internal_port, internal_protocol, external_port, external_protocol)
                    for (internal_port, internal_protocol), (external_port, external_protocol) in zip(
                        zip(instance_ports, instance_protocols), zip(elb_ports, elb_protocols))
                ]
            )
        )
        self.elb2.create_target_group.assert_called_with(
            Name="unittestenv-mhcunit",
            Protocol='TCP',
            Port=80,
            VpcId=TEST_VPC_ID,
            HealthCheckProtocol="TCP",
            HealthCheckPort="80"
        )

    def test_create_target_group_ssl(self):
        """Test creating a group that is SSL"""
        self.elb2.describe_target_groups.side_effect = EC2ResponseError(
            status="mockstatus",
            reason="mockreason"
        )

        instance_protocols = ('SSL',)
        instance_ports = (80,)
        elb_protocols = ('SSL',)
        elb_ports = (80,)

        self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
            port_config=DiscoELBPortConfig(
                [
                    DiscoELBPortMapping(internal_port, internal_protocol, external_port, external_protocol)
                    for (internal_port, internal_protocol), (external_port, external_protocol) in zip(
                        zip(instance_ports, instance_protocols), zip(elb_ports, elb_protocols))
                ]
            )
        )
        self.elb2.create_target_group.assert_called_with(
            Name="unittestenv-mhcunit",
            Protocol='TLS',
            Port=80,
            VpcId=TEST_VPC_ID,
            HealthCheckProtocol="TCP",
            HealthCheckPort="80"
        )

    def test_tags_transformation(self):
        """Tests if tags are converted to the right format"""
        tags = {
            "application": "fake-app-tag",
            "environment": "fake-environment"
        }
        expected_tags = [
            {
                'Value': "fake-app-tag",
                'Key': "application"
            },
            {
                'Value': "fake-environment",
                'Key': "environment"
            }
        ]
        actual_tags = self.disco_elb.add_tags_to_target_groups(
            target_groups=["mock_target_group"],
            tags=tags
        )
        self.assertEqual(sorted(actual_tags), sorted(expected_tags))

    def test_add_tags_existing_tg(self):
        """ Tests if add tags is called for existing tg"""
        describe_call = {
            "TargetGroups": [
                {"TargetGroupArn": "mock_target_group"},
            ]
        }
        self.elb2.describe_target_groups.return_value = describe_call
        self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
            tags={"fake-key": "fake-value"}
        )
        self.elb2.add_tags.assert_called_with(
            ResourceArns=["mock_target_group"],
            Tags=[{'Key': "fake-key", 'Value': "fake-value"}]
        )

    def test_add_tags_created_tg(self):
        """ Tests if add tags is called for new tg"""
        self.elb2.describe_target_groups.side_effect = EC2ResponseError(
            status="mockstatus",
            reason="mockreason"
        )
        self.elb2.create_target_group.return_value = {'TargetGroups': [{'TargetGroupArn': "fake-tg-arn"}]}
        self.disco_elb.get_or_create_target_group(
            environment=TEST_ENV_NAME,
            hostclass=TEST_HOSTCLASS,
            vpc_id=TEST_VPC_ID,
            tags={"fake-key": "fake-value"}
        )
        self.elb2.create_target_group.assert_called_with(
            Name="unittestenv-mhcunit",
            Protocol='HTTP',
            Port=80,
            VpcId=TEST_VPC_ID,
            HealthCheckProtocol="HTTP",
            HealthCheckPort="80",
            HealthCheckPath="/"
        )
        self.elb2.add_tags.assert_called_with(
            ResourceArns=["fake-tg-arn"],
            Tags=[{'Key': "fake-key", 'Value': "fake-value"}]
        )
